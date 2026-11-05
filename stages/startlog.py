"""What a Magnetar log says about why a server never reached the plugin. Used by the magnetar, legacy and behaviour stages."""
import datetime, json, re, time, urllib.request

import githubtoken


def rate_limit():
    url = 'https://api.github.com/rate_limit'
    request = urllib.request.Request(url, headers={'User-Agent': 'PluginCheck', **githubtoken.headers(url)})
    return json.load(urllib.request.urlopen(request, timeout=20))['resources']['core']


def rate_limit_reset():
    """When GitHub's limit resets, as text, or None. Asking costs nothing against the limit."""
    try:
        core = rate_limit()
        return datetime.datetime.fromtimestamp(core['reset']).strftime('%H:%M')
    except (OSError, ValueError, KeyError):
        return None


def rate_limit_wait():
    """Seconds until GitHub's limit resets, with a margin. Falls back to the whole hour if GitHub cannot say."""
    try:
        core = rate_limit()
        return max(int(core['reset'] - time.time()) + 15, 15) if core['remaining'] < 10 else 15
    except (OSError, ValueError, KeyError):
        return 3600


def loader_stopped(text):
    """The reason Magnetar gave up before starting the plugin, or None. That says nothing about the plugin itself."""
    if 'Pulsar cannot continue loading' not in text:
        return None
    core = re.search(r'Failed to load core plugin (\S+)', text)
    what = f'its core plugin {core.group(1)}' if core else 'a core plugin'
    if re.search(r'rate limit exceeded', text, re.I):
        reset = rate_limit_reset()
        when = f' It resets at {reset}, so run it again after that.' if reset else ' Run it again once it resets.'
        return f"GitHub's rate limit stopped Magnetar loading {what}, so the plugin never started.{when}"
    return f'Magnetar could not load {what} and stopped, so the plugin never started. Read info.log for the cause.'


def last_lines(text, count=5):
    """The end of a log, for a server that stopped with no known cause."""
    return ' | '.join(line.strip()[:160] for line in text.splitlines()[-count:] if line.strip())


def plugin_problem(text, registered, state):
    """What is wrong with the plugin going by the log, as (kind, text), or None."""
    enabled = re.search(r'Enabled Plugins: (.*)', text)
    if enabled and enabled.group(1).strip() == 'None':
        return 'plugin load', 'the plugin was not enabled, so nothing about it was tested'
    if not enabled:
        tail = last_lines(text) or 'the log is empty'
        return 'plugin load', f'Magnetar stopped ({state}) before it listed enabled plugins, so nothing about the plugin was tested. Last lines: {tail}'
    if not registered:
        return 'command registration', 'no command modules were registered by the plugin'
    return None
