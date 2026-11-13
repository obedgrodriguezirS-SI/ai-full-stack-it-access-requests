"""SQLite single-host durable attempts with leases, fencing, and atomic exports."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.changed = threading.Condition()
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                    raw BLOB NOT NULL, settings TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, token TEXT, lease_until REAL,
                    record TEXT, created_at REAL NOT NULL, finished_at REAL);
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY, latest INTEGER NOT NULL, current INTEGER);
                CREATE INDEX IF NOT EXISTS work ON attempts(state, lease_until);
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, document_id, raw, settings, force=False):
        digest = hashlib.sha256(raw).hexdigest()
        with self.connection(write=True) as db:
            latest = db.execute('SELECT a.* FROM documents d JOIN attempts a ON a.id=d.latest WHERE d.document_id=?', (document_id,)).fetchone()
            if not force and latest and latest['source_sha256']==digest and latest['fingerprint']==settings['fingerprint']:
                if latest['state'] in ('pending','running') or (latest['record'] and json.loads(latest['record'])['status']!='error'):
                    return latest['id']
            attempt_id = db.execute('INSERT INTO attempts(document_id,source_sha256,raw,settings,fingerprint,state,created_at) VALUES(?,?,?,?,?,?,?)',
                (document_id,digest,raw,encoded(settings),settings['fingerprint'],'pending',time.time())).lastrowid
            db.execute('INSERT INTO documents(document_id,latest) VALUES(?,?) ON CONFLICT(document_id) DO UPDATE SET latest=excluded.latest', (document_id,attempt_id))
            return attempt_id

    @staticmethod
    def job(row):
        result = dict(row)
        result['settings'] = json.loads(result['settings'])
        result['record'] = json.loads(result['record']) if result['record'] else None
        return result

    def retry(self, document_id):
        with self.connection() as db:
            row = db.execute('SELECT a.* FROM documents d JOIN attempts a ON a.id=d.latest WHERE d.document_id=?',(document_id,)).fetchone()
        if row is None:
            raise ValueError('unknown document; inspect queue for a document_id')
        return self.register(document_id, row['raw'], json.loads(row['settings']), force=True)

    def claim(self, now=None, lease_seconds=300):
        now = time.time() if now is None else now
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM attempts WHERE state='pending' OR (state='running' AND lease_until<=?) ORDER BY id LIMIT 1",(now,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE attempts SET state='running',token=?,lease_until=? WHERE id=?",(token,now+lease_seconds,row['id']))
            result = self.job(row)
            result.update(state='running',token=token,lease_until=now+lease_seconds)
            return result

    def renew(self, job, lease_seconds=300):
        with self.connection(write=True) as db:
            return db.execute("UPDATE attempts SET lease_until=? WHERE id=? AND token=? AND state='running'",
                (time.time()+lease_seconds,job['id'],job['token'])).rowcount == 1

    def finish(self, job, record):
        with self.connection(write=True) as db:
            changed = db.execute("UPDATE attempts SET state='completed',record=?,finished_at=?,lease_until=NULL WHERE id=? AND token=? AND state='running'",
                (encoded(record),time.time(),job['id'],job['token'])).rowcount
            if not changed:
                return False
            db.execute('UPDATE documents SET current=? WHERE document_id=? AND latest=?',(job['id'],job['document_id'],job['id']))
        with self.changed:
            self.changed.notify_all()
        return True

    def wait_for_completed(self, document_id, timeout=5):
        deadline = time.monotonic()+timeout
        with self.changed:
            while time.monotonic()<deadline:
                if any(r['document_id']==document_id for r in self.snapshot()['records']):
                    return True
                self.changed.wait(max(0,deadline-time.monotonic()))
        return False

    def snapshot(self):
        with self.connection() as db:
            db.execute('BEGIN')
            records = [json.loads(r[0]) for r in db.execute('SELECT a.record FROM documents d JOIN attempts a ON a.id=d.current ORDER BY d.document_id')]
            pending = [dict(id=r['id'],document_id=r['document_id'],source_sha256=r['source_sha256'],state=r['state'],lease_until=r['lease_until'],settings=json.loads(r['settings']))
                for r in db.execute("SELECT * FROM attempts WHERE state!='completed' ORDER BY id")]
        return {'records':records,'pending':pending}

    def history(self, document_id=None):
        with self.connection() as db:
            query = 'SELECT * FROM attempts'
            params = ()
            if document_id is not None:
                query += ' WHERE document_id=?'; params=(document_id,)
            rows = db.execute(query+' ORDER BY id',params).fetchall()
        return [dict(attempt_id=r['id'],document_id=r['document_id'],source_sha256=r['source_sha256'],
                     settings=json.loads(r['settings']),state=r['state'],created_at=r['created_at'],
                     finished_at=r['finished_at'],record=json.loads(r['record']) if r['record'] else None,
                     source_utf8=bytes(r['raw']).decode('utf-8',errors='replace')) for r in rows]

    def export(self, output):
        """Publish immutable files via one atomic current symlink; pin it when reading a pair."""
        output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
        with (output/'.export.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # One DB read transaction for all export contents, including history.
            with self.connection() as db:
                db.execute('BEGIN')
                rows=db.execute('SELECT * FROM attempts ORDER BY id').fetchall()
                current={r[0] for r in db.execute('SELECT current FROM documents WHERE current IS NOT NULL')}
                records=[json.loads(r['record']) for r in rows if r['id'] in current]
                records.sort(key=lambda r:r['document_id'])
                history=[dict(attempt_id=r['id'],document_id=r['document_id'],source_sha256=r['source_sha256'],settings=json.loads(r['settings']),state=r['state'],record=json.loads(r['record']) if r['record'] else None) for r in rows if r['id'] not in current]
            generation=output/'generations'/uuid.uuid4().hex;generation.mkdir(parents=True)
            queue=[{k:r[k] for k in ('document_id','source_sha256','attempt_id','status','owner','next_action','error','response_source')} for r in records]
            contents={'extractions.jsonl':''.join(encoded(r)+'\n' for r in records),
                      'queue.json':json.dumps(queue,ensure_ascii=False,indent=2)+'\n',
                      'history.jsonl':''.join(encoded(r)+'\n' for r in history)}
            for name,content in contents.items():
                with (generation/name).open('w',encoding='utf-8') as file:
                    file.write(content);file.flush();os.fsync(file.fileno())
            self._sync_directory(generation)
            self._sync_directory(generation.parent)
            temporary=output/('.current-'+uuid.uuid4().hex)
            temporary.symlink_to(generation.relative_to(output),target_is_directory=True)
            os.replace(temporary,output/'current')
            for name in contents:
                target=output/name
                if not target.is_symlink():
                    temp=output/('.link-'+uuid.uuid4().hex);temp.symlink_to(Path('current')/name);os.replace(temp,target)
            self._sync_directory(output)
            return generation

    @staticmethod
    def _sync_directory(path):
        fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
