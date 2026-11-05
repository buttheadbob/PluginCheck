"""An optional GitHub token, for many more requests an hour than the anonymous 60.

Keep it in github-token.txt beside check.py, which git ignores, or in PULSAR_GITHUB_TOKEN. Use a fine-grained token with read access to
public repositories and nothing else. A container runs plugin code, and a plugin can read its own environment, so treat any token given
to one as readable by that plugin. It is never put on a command line or in a log.
"""
import os, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, '..', 'github-token.txt')
NAME = 'PULSAR_GITHUB_TOKEN'
OFF = 'PLUGINCHECK_TOKEN_REJECTED'
HOSTS = ('https://api.github.com/', 'https://codeload.github.com/')


def token():
    if os.environ.get(OFF):
        return None
    if os.environ.get(NAME):
        return os.environ[NAME].strip()
    try:
        return open(FILE, encoding='utf-8').read().strip() or None
    except OSError:
        return None


def headers(url):
    """Sent to GitHub only, so no other site ever sees it."""
    value = token()
    return {'Authorization': f'Bearer {value}'} if value and url.startswith(HOSTS) else {}


def check():
    """A token GitHub refuses makes every request fail, which is worse than having none, so an expired one is set aside for this run."""
    value = token()
    if not value:
        return None
    url = 'https://api.github.com/rate_limit'
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PluginCheck', **headers(url)}), timeout=20).read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            os.environ[OFF] = '1'
            return 'GitHub refused the token, so it has probably expired. This run goes on without it. Make a new one in github-token.txt.'
    except OSError:
        pass
    return None


def for_container():
    """Arguments for docker run, and the environment to run it with. Named only, so the value stays off the command line."""
    value = token()
    return (['-e', NAME], {**os.environ, NAME: value}) if value else ([], None)
