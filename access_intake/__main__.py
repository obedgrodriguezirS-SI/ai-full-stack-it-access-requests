"""Documented CLI: intake, queue, show, retry, resume, export, accuracy."""
import argparse
import json
from pathlib import Path
import sys

from access_intake.accuracy import markdown
from access_intake.engine import Engine, settings_for
from access_intake.store import Store
from access_intake.validation import ROOT


def parser():
    result=argparse.ArgumentParser(description='Prepare access requests for human review')
    commands=result.add_subparsers(dest='command',required=True)
    for name in ('intake','queue','show','retry','resume','export'):
        command=commands.add_parser(name)
        command.add_argument('--state',type=Path,default=Path('.state/intake.sqlite'))
        if name in ('intake','retry','resume','export'):
            command.add_argument('--output',type=Path,default=Path('deliverables'))
        if name in ('intake','retry','resume'):
            command.add_argument('--workers',type=int,default=4)
        if name in ('show','retry'):
            command.add_argument('document_id')
        if name=='intake':
            command.add_argument('inputs',type=Path)
            command.add_argument('--provider',choices=('mock','openai'),default='mock')
            command.add_argument('--fixtures',type=Path,default=ROOT/'tests/mock_responses')
            command.add_argument('--model',default='gpt-4.1-mini-2025-04-14')
            command.add_argument('--temperature',type=float,default=0)
            command.add_argument('--max-output-tokens',type=int,default=2400)
            command.add_argument('--timeout',type=float,default=60)
            command.add_argument('--policy',type=Path,default=ROOT/'materials/office-policy.md')
    accuracy=commands.add_parser('accuracy')
    accuracy.add_argument('--expected',type=Path,required=True)
    accuracy.add_argument('--actual',type=Path,required=True)
    accuracy.add_argument('--output',type=Path,required=True)
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='accuracy':
            expected=json.loads(args.expected.read_text(encoding='utf-8'))
            actual=[json.loads(line) for line in args.actual.read_text(encoding='utf-8').splitlines() if line.strip()]
            text,report=markdown(expected,actual)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(text,encoding='utf-8')
            print(json.dumps({k:report[k] for k in ('matched','denominator','routing_matched','routing_denominator','failed_or_missing')}))
            return 0 if report['matched']==report['denominator'] and report['routing_matched']==report['routing_denominator'] and not report['unexpected_documents'] else 1
        if args.command in ('queue','show') and not args.state.is_file():
            raise ValueError('state database does not exist; start intake first')
        store=Store(args.state)
        engine=Engine(store)
        if args.command=='queue':
            print(json.dumps(store.snapshot(),ensure_ascii=False,indent=2));return 0
        if args.command=='show':
            history=store.history(args.document_id)
            if not history:raise ValueError('unknown document_id')
            print(json.dumps({'document_id':args.document_id,'history':history},ensure_ascii=False,indent=2));return 0
        if args.command=='intake':
            settings=settings_for(provider=args.provider,model=args.model,fixtures=args.fixtures,policy=args.policy,
                temperature=args.temperature,max_output_tokens=args.max_output_tokens,timeout=args.timeout)
            paths=sorted(args.inputs.glob('*.json')) if args.inputs.is_dir() else [args.inputs]
            if not paths:raise ValueError('input directory contains no JSON exports')
            engine=Engine(store,settings=settings)
            engine.enroll(paths)
        if args.command=='retry':engine.retry(args.document_id)
        if args.command in ('intake','retry','resume'):engine.run(args.workers)
        generation=store.export(args.output)
        print(json.dumps({'generation':str(generation),'records':len(store.snapshot()['records']),
                          'pending':len(store.snapshot()['pending'])}))
        return 0
    except KeyboardInterrupt:
        print('intake interrupted; completed records are durable; resume after lease expiry',file=sys.stderr)
        return 130
    except (ValueError,OSError) as exc:
        # No arbitrary input/provider body text or credentials in operational logs.
        print(f'{type(exc).__name__}: command could not complete; check input paths, arguments and state; use --help',file=sys.stderr)
        return 2


if __name__=='__main__':
    sys.exit(main())
