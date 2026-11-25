"""Trusted GitHub transport, never execute target code."""
import json
import os
import urllib.request
from .common import REPO


def api(path, method='GET', body=None):
    req = urllib.request.Request('https://api.github.com/'+path,
        None if body is None else json.dumps(body).encode(),
        {'Authorization':'Bearer '+os.environ['GH_TOKEN'], 'Accept':'application/vnd.github+json',
         'Content-Type':'application/json', 'X-GitHub-Api-Version':'2022-11-28'}, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def comments(issue):
    result = []
    for page in range(1,101):
        batch=api(f'repos/{REPO}/issues/{issue}/comments?per_page=100&page={page}')
        result.extend(batch)
        if len(batch)<100:
            return result
    raise ValueError('Comment pagination limit exceeded')
