"""Run once in GitHub Actions. Secrets must be environment variables."""
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / 'monitor_state.json'
TARGETS = ('TimPayPay', 'HWu5KvOzXiCvokT')
WEBHOOKS = {
    'timpaypay': os.getenv('DISCORD_WEBHOOK_URL', ''),
    'hwu5kvozxicvokt': os.getenv('DISCORD_WEBHOOK_URL_2', ''),
}
AFID = os.getenv('MERCARI_AFID', '')
BEARER = os.getenv('X_BEARER_TOKEN', '')
QUERY = '(' + ' OR '.join(f'from:{name}' for name in TARGETS) + ') is:reply url:"jp.mercari.com"'
URL_RE = re.compile(r'https?://[^\s<>"\']+')
ITEM_RE = re.compile(r'/item/(m\d+)')
JST = ZoneInfo('Asia/Tokyo')
# Weekday 0=Mon ... 6=Sun; end can be on the next day.
SCHEDULE = {
    0: (time(8, 0), time(0, 30)),
    1: (time(8, 0), time(0, 30)),
    2: (time(8, 0), time(0, 30)),
    3: (time(8, 0), time(0, 30)),
    4: (time(8, 0), time(2, 0)),
    5: (time(9, 0), time(2, 0)),
    6: (time(9, 0), time(0, 30)),
}


def active_now(now):
    for offset in (0, 1):
        day = (now - timedelta(days=offset)).date()
        start, end = SCHEDULE[day.weekday()]
        begins = datetime.combine(day, start, tzinfo=JST)
        ends = datetime.combine(day + timedelta(days=int(end <= start)), end, tzinfo=JST)
        if begins <= now < ends:
            return True
    return False


def load_state():
    if not STATE_PATH.exists():
        return {'active': False, 'since_id': None, 'start_time': None, 'processed_items': []}
    data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    return {
        'active': bool(data.get('active', False)),
        'since_id': data.get('since_id'),
        'start_time': data.get('start_time'),
        'processed_items': data.get('processed_items', []),
    }


def save_state(state):
    temp = STATE_PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATE_PATH)


def extract(tweet):
    parent = next((str(r['id']) for r in tweet.get('referenced_tweets', [])
                   if r.get('type') == 'replied_to' and r.get('id')), None)
    urls = URL_RE.findall(tweet.get('text', ''))
    for entry in tweet.get('entities', {}).get('urls', []):
        urls.extend(entry[k] for k in ('unwound_url', 'expanded_url') if entry.get(k))
    items = {}
    for url in urls:
        url = url.rstrip('.,!?)]')
        p = urlsplit(url)
        match = ITEM_RE.fullmatch(p.path)
        if p.scheme == 'https' and p.hostname == 'jp.mercari.com' and match:
            items.setdefault(match.group(1), url)
    return parent, items


def send_item(tweet, parent, url):
    author = tweet['_author_username'].lower()
    webhook = WEBHOOKS.get(author)
    if not webhook:
        raise RuntimeError(f'Missing webhook for {author}')

    p = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(
        p.query, keep_blank_values=True) if k != 'afid']
    pairs.insert(0, ('afid', AFID))

    affiliate = urlunsplit((
        p.scheme, p.netloc, p.path,
        urlencode(pairs), p.fragment
    ))

    intent = 'https://twitter.com/intent/tweet?' + urlencode({
        'text': affiliate,
        'in_reply_to': parent
    })

    embed = {
        'author': {
            'name': f'メルカリ監視BOT | @{tweet["_author_username"]}'
        },
        'title': '🔔 メルカリリンクを検出！',
        'color': 3447003,
        'fields': [
            {
                'name': '📌 元ツイート',
                'value': f'[元ツイートを開く](https://x.com/i/status/{parent})',
                'inline': False
            },
            {
                'name': '🔗 Xで返信する',
                'value': f'[返信画面を開く]({intent})',
                'inline': False
            }
        ]
    }

    response = requests.post(webhook, json={
        'content': '@everyone',
        'embeds': [embed],
        'allowed_mentions': {'parse': ['everyone']}
    }, timeout=20)

    response.raise_for_status()
def fetch_recent(state):
    params = {
        'query': QUERY, 'tweet.fields': 'referenced_tweets,entities,author_id',
        'expansions': 'author_id', 'user.fields': 'username', 'max_results': 100,
    }
    if state['since_id']:
        params['since_id'] = state['since_id']
    else:
        params['start_time'] = state['start_time']
    all_tweets = []
    for _ in range(5):
        response = requests.get('https://api.x.com/2/tweets/search/recent',
                                headers={'Authorization': f'Bearer {BEARER}'},
                                params=params, timeout=35)
        if not response.ok:
            print(f'X API error: HTTP {response.status_code}')
        response.raise_for_status()
        payload = response.json()
        if payload.get('errors'):
            raise RuntimeError('X API partial error; state unchanged')
        authors = {str(u['id']): u['username'] for u in payload.get('includes', {}).get('users', [])}
        for tweet in payload.get('data', []):
            tweet['_author_username'] = authors.get(str(tweet.get('author_id', '')), '')
            all_tweets.append(tweet)
        token = payload.get('meta', {}).get('next_token')
        if not token:
            return all_tweets
        params['next_token'] = token
    raise RuntimeError('Too many pages; state unchanged')


def main():
    now = datetime.now(JST)
    state = load_state()
    if not active_now(now):
        if state['active']:
            state['active'] = False
            state['since_id'] = None
            state['start_time'] = None
            save_state(state)
        print('Outside JST monitoring hours; X API not called.')
        return
    if not all(WEBHOOKS.values()) or not AFID or not BEARER:
        raise RuntimeError('Missing GitHub Actions secrets')
    if not state['active']:
        # A new session starts now; avoid retrieving posts from sleeping hours.
        state.update({'active': True, 'since_id': None,
                      'start_time': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        save_state(state)
        print('New monitoring session initialized; first query on next scheduled run.')
        return
    tweets = fetch_recent(state)
    tweets.sort(key=lambda t: int(t['id']))
    processed = set(state['processed_items'])
    notified = 0
    for tweet in tweets:
        ident = str(tweet['id'])
        author = tweet.get('_author_username', '').lower()
        if author not in WEBHOOKS:
            raise RuntimeError(f'Unknown author for post {ident}; state not advanced')
        parent, items = extract(tweet)
        if parent:
            for item_id, item_url in items.items():
                if item_id in processed:
                    print('Duplicate Mercari item skipped:', item_id)
                    continue
                send_item(tweet, parent, item_url)
                processed.add(item_id)
                state['processed_items'] = sorted(processed)
                save_state(state)
                notified += 1
        state['since_id'] = ident
        save_state(state)
    print(f'Checked {len(tweets)} X posts; sent {notified} Discord notifications.')


if __name__ == '__main__':
    main()
