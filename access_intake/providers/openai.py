"""OpenAI Responses API over injectable stdlib HTTP. Secrets never enter state/logs."""
import json
import os
import urllib.request
from urllib.error import HTTPError, URLError

from access_intake.prompt import PROMPT
from access_intake.validation import SCHEMA, strict_json


class ProviderError(RuntimeError):
    """Safe operational error containing no server body or credentials."""


def http_transport(url, headers, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers, method='POST')
    # Do not follow redirects carrying credentials to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return strict_json(response.read().decode('utf-8'))


class OpenAIProvider:
    def __init__(self, settings, transport=None, getenv=None):
        self.settings = settings
        self.transport = transport or http_transport
        self.getenv = getenv or os.environ.get
        self.response_model = None

    def request(self, message):
        return {'model': self.settings['model'], 'store': False,
                'temperature': self.settings['temperature'],
                'max_output_tokens': self.settings['max_output_tokens'],
                'instructions': PROMPT + '\nOffice policy:\n' + self.settings['policy_text'],
                'input': [{'role':'user','content':message}],
                'text': {'format': {'type':'json_schema','name':'access_request',
                                    'strict':True,'schema':SCHEMA}}}

    def evidence(self, message):
        return {'provider':'openai', 'observation':'illustrative request; no live call',
                'url':self.settings['endpoint'], 'headers':{'Authorization':'Bearer [REDACTED]'},
                'request':self.request(message)}

    def extract(self, document_id, message):
        key = self.getenv('API_KEY')
        if not key:
            raise ProviderError('API_KEY is required for OpenAI; set it and retry')
        try:
            reply = self.transport(self.settings['endpoint'],
                {'Authorization':'Bearer '+key,'Content-Type':'application/json'},
                self.request(message), self.settings['timeout'])
        except HTTPError as exc:
            if exc.code == 429:
                raise ProviderError('provider rate limited (HTTP 429); wait then retry') from None
            if exc.code >= 500:
                raise ProviderError(f'provider unavailable (HTTP {exc.code}); retry later') from None
            raise ProviderError(f'provider HTTP {exc.code}; check configuration then retry') from None
        except (TimeoutError, URLError):
            raise ProviderError('provider timeout or connection failure; retry later') from None
        except Exception:
            raise ProviderError('provider transport failure; check configuration then retry') from None
        # The response's model identifies the actual producer when an alias was requested.
        if isinstance(reply, dict) and isinstance(reply.get('model'), str):
            self.response_model = reply['model']
        return reply
