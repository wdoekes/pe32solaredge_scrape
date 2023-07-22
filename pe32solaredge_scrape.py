#!/usr/bin/env python3
# """
# SolarEdge logging scrape
#
# Usage::
#
#     $ cat > config.yaml < EOF
#     solaredge_web:
#       api_v3_site_url:
#         https://monitoring.solaredge.com/solaredge-apigw/api/v3/sites/NUMBER
#       http_referer:
#         https://monitoring.solaredge.com/solaredge-web/p/site/NUMBER/
#       http_user_agent: Mozilla/5.0 (X11; Linux x86_64) ...
#       cookies:
#         SolarEdge_Client-1.6: 83b...
#         SolarEdge_SSO-1.4: 011...
#         SolarEdge_Field_ID: NUMBER
#         # Irrelevant. Even for en_US we get user-configured values.
#         SolarEdge_Locale: nl_NL
#         # This one changes every now and then.
#         # (It IS needed. Also, it is readable B64 without trailing '='.)
#         SPRING_SECURITY_REMEMBER_ME_COOKIE: d2F...
#         # This one changes every time. It is NOT needed for initial call.
#         #JSESSIONID: 6380...
#     database:
#       dsn:
#         host: dbhost
#         user: dbuser
#         database: dbname
#         password: cGFzc3dvcmQK
#     EOF
#
#     $ python3 pe32solaredge_scrape.py
#     {'currentPower': 306.36063,
#      'lastDayEnergy': 1446.0,
#      'lastUpdateTime': datetime.datetime(2022, 2, 6, 14, 33, tzinfo=<UTC>),
#      'lifeTimeEnergy': 4352049.0}
#
#     $ python3 pe32solaredge_scrape.py insert
#     (inserts into db)
#
# This is work in progress.
# """
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
import yaml

try:
    # python3-pytz on Ubuntu
    import pytz
    TZINFO = pytz.timezone('Europe/Amsterdam')

    def dt_naive_to_local(naive_dt):
        try:
            return TZINFO.localize(naive_dt, is_dst=None)
        except pytz.exceptions.AmbiguousTimeError:
            # Don't care; this is during night time anyway.
            return TZINFO.localize(naive_dt, is_dst=False)

    def dt_local_to_utc(local_dt):
        return local_dt.astimezone(pytz.utc)

    def dt_unixtime(timeobj):
        return int(timeobj.timestamp())
except ImportError:
    # python3-arrow on Raspbian
    import arrow
    from dateutil.tz import gettz
    TZFILE = gettz('Europe/Amsterdam')

    def dt_naive_to_local(naive_dt):
        # XXX: what about ambiguous time?
        return arrow.get(naive_dt, TZFILE)

    def dt_local_to_utc(local_dt):
        return local_dt.to('UTC')

    def dt_unixtime(timeobj):
        return timeobj.timestamp

try:
    import psycopg2
except ImportError:
    pass

log = logging.getLogger()

BINDIR = os.path.dirname(__file__)
CONFDIR = SPOOLDIR = BINDIR

# config.yaml, to configure ATAG One API credentials
# > login:
# >   Email: user@domain.tld
# >   Password: YmFzZTY0X2VuY29kZWRfcGFzc3dvcmQ=
CONFIG = os.path.join(CONFDIR, 'config.yaml')

# Optionally we can override the CONFIG and SPOOLDIR using env.
if os.environ.get('SOLAREDGE_SCRAPE_CONFIG'):
    CONFIG = os.environ['SOLAREDGE_SCRAPE_CONFIG']
if os.environ.get('SOLAREDGE_SCRAPE_RUNDIR'):
    SPOOLDIR = os.environ['SOLAREDGE_SCRAPE_RUNDIR']

# cookies.json, for temporary cookie storage
COOKIE_JAR = os.path.join(SPOOLDIR, 'cookies.json')


def load_config_yaml():
    """
    Load config.yaml.

    For SolarEdge monitoring login, it requires:

      solaredge_web:
        overview_js_url: ...
        cookies:
          JSESSIONID: ...
          foo: bar
          baz: ...

    You'll need to borrow the cookies from another login.

    For push to Postgres/Timescale, also:

      database:
        dsn:
          host: 127.0.0.1
          user: dbuser
          dbname: database
          password: BASE64_ENCODED_PASSWORD
    """
    config = {}
    try:
        with open(CONFIG) as fp:
            config = yaml.safe_load(fp.read())

        # Mandatory.
        assert config.get('solaredge_web', {}).get('api_v3_site_url')
        assert not config.get('solaredge_web', {}).get('http_referrer'), 'typo'
        config['solaredge_web']['http_referer'] = (
            config['solaredge_web'].get('http_referer', None))
        config['solaredge_web']['http_user_agent'] = (
            config['solaredge_web'].get('http_user_agent', (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/88.0.4324.96 Safari/537.36')))
        assert config.get('solaredge_web', {}).get('cookies')
        # Cast every cookie to string, in case the yaml gave us ints or
        # floats.
        for k, v in config['solaredge_web']['cookies'].items():
            config['solaredge_web']['cookies'][k] = str(v)
    except Exception as e:
        log.exception('Problem in config %r', config)
        raise

    # Base64 decode database.dsn.password.
    db_password = config.get('database', {}).get('dsn', {}).get('password')
    if db_password:
        config['database']['dsn']['password'] = base64.b64decode(
            db_password).decode('ascii')

    return config


def restore_session():
    sess = requests.Session()
    try:
        with open(COOKIE_JAR) as fp:
            jar = json.load(fp)
    except FileNotFoundError:
        pass
    else:
        sess.cookies = requests.cookies.cookiejar_from_dict(jar)
    return sess


def store_session(sess):
    jar = requests.utils.dict_from_cookiejar(sess.cookies)
    with open(COOKIE_JAR, 'w') as fp:
        json.dump(jar, fp)
        fp.write('\n')


def parse_api_v3_js(text):
    """
    {
      "siteClassType": "DEFAULT",
      "fieldOverview": {
        "uris": {...},
        "fieldOverview": {
          "lastUpdateTime": "2021-01-31 10:28:12.0",
          "lifeTimeData": {
            "fieldId": "NUMBER",
            "energy": 36180,
            ...
          "lastDayData": {
            "fieldId": "NUMBER",
            "energy": 436,
            ...
          "solarField": {
            "id": "NUMBER",
            "name": "UserX 0123456",
            ...
          "currentPower": {
            "currentPower": 179.59799,
            "unit": "W"
            ...
    """
    js = json.loads(text)
    fo = js['fieldOverview']['fieldOverview']
    ret = {}
    ret['lastUpdateTime'] = fo['lastUpdateTime']
    ret['lastUpdateTime'] = ret['lastUpdateTime'].split('.', 1)[0]
    assert len(ret['lastUpdateTime']) == 19, ret
    naive_dt = datetime.strptime(ret['lastUpdateTime'], '%Y-%m-%d %H:%M:%S')
    local_dt = dt_naive_to_local(naive_dt)
    utc_dt = dt_local_to_utc(local_dt)
    ret['lastUpdateTime'] = utc_dt
    ret['lifeTimeEnergy'] = fo['lifeTimeData']['energy']        # Wh
    ret['lastDayEnergy'] = fo['lastDayData']['energy']          # Wh
    ret['currentPower'] = fo['currentPower']['currentPower']    # W
    assert fo['currentPower']['unit'] == 'W', fo
    return ret


def fetch_api_v3_site():
    web = load_config_yaml()['solaredge_web']
    sess = restore_session()
    if not sess.cookies:
        sess.cookies = requests.cookies.cookiejar_from_dict(web['cookies'])

    headers = {
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': '*/*',
        # 'Connection': 'close',
    }
    if web['http_user_agent']:
        headers['User-Agent'] = web['http_user_agent']
    if web['http_referer']:
        headers['Referer'] = web['http_referer']

    resp = sess.get(web['api_v3_site_url'], headers=headers)
    if (resp.status_code != 200
            or 'currentPower' not in resp.text):
        sess.cookies.clear()  # wipe stale cookies
        # reset.
        sess.cookies = requests.cookies.cookiejar_from_dict(web['cookies'])
        # retry once.
        resp2 = sess.get(web['api_v3_site_url'], headers=headers)
        if (resp2.status_code != 200
                or 'currentPower' not in resp.text):
            raise ValueError((resp, resp2, resp.text, resp2.text))
        resp = resp2
    store_session(sess)
    return resp.text


def fetch_cached_api_v3_site(clear_cache=False):
    """
    Return cached api v3 site json.

    The clear_cache is a hack so we flush the cache manually, while
    keeping the caching method internal to this function.

      {
        "siteClassType": "DEFAULT",
        "fieldOverview": {
      ...
          "fieldOverview": {
      ...
            "currentPower": {
              "currentPower": 179.59799,
              "unit": "W"
            },
      ...
    """
    cache_file = os.path.join(SPOOLDIR, 'api_v3_site.js')
    try:
        if clear_cache:
            raise FileNotFoundError()  # pretend it wasn't there
        with open(cache_file) as fp:
            st = os.fstat(fp.fileno())
            age = (time.time() - st.st_mtime)
            text = fp.read()
        if text.lstrip()[0] != '{':
            # Broken cache. Retry immediately.
            raise ValueError()
    except (FileNotFoundError, ValueError):
        text = fetch_api_v3_site()
        with open(cache_file, 'w') as fp:
            fp.write(text)
        age = 0
    return text, age


def fetch_reasonably_fresh_data():
    # Get recent copy first.
    text, age = fetch_cached_api_v3_site(clear_cache=False)
    parsed = parse_api_v3_js(text)
    # While current power is 0, only query every 15 minutes.
    if parsed['currentPower'] == 0 and age < (15 * 60):
        return parsed, False

    text, age = fetch_cached_api_v3_site(clear_cache=True)
    parsed = parse_api_v3_js(text)
    return parsed, True


def insert_latest_into_db():
    """
    Run every minute.
    """
    def execute_or_ignore(c, q):
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(query)
        except psycopg2.IntegrityError:
            pass

    parsed, fresh = fetch_reasonably_fresh_data()
    if not fresh:
        exit()

    config = load_config_yaml()
    conn = psycopg2.connect(**config['database']['dsn'])

    # with conn.cursor() as cursor:
    #     cursor.execute('SELECT NOW();');
    #     now = cursor.fetchall()[0][0]
    # dt = now

    dt = parsed['lastUpdateTime'].strftime('%Y-%m-%d %H:%M:%S')
    table_loc_vals = (
        # ('power', 14, parsed['currentPower']),
        # ('power_kwh', 14, parsed['lifeTimeEnergy'] / 1000.0),
        ('power_kwh', 15, parsed['lastDayEnergy'] / 1000.0),
    )
    for table, label_id, value in table_loc_vals:
        query = (
            f"INSERT INTO {table} (time, location_id, value) VALUES "
            f"('{dt}'::timestamptz, {label_id}, {value});")
        execute_or_ignore(conn, query)


def fetch_and_publish():
    while True:
        parsed, fresh = fetch_reasonably_fresh_data()
        if fresh:
            latest_json = os.path.join(SPOOLDIR, 'latest.json')
            with open(latest_json + '.new', 'w') as fp:
                fp.write(json.dumps({
                    'inst_solar_pwr': parsed['currentPower'],
                    'solar_act': parsed['lifeTimeEnergy'],
                    'solar_act_day': parsed['lastDayEnergy'],
                    'last_update': dt_unixtime(parsed['lastUpdateTime']),
                }) + '\n')
            os.rename(latest_json + '.new', latest_json)

            with open(latest_json) as fp:
                log.info('WROTE latest.json: %r', fp.read())

            # XXX: publish
            log.warning(
                'FIXME: publish not implemented yet. Data in %r',
                latest_json)
        log.info('SLEEPING for 400')
        time.sleep(400)


def main():
    from pprint import pprint
    text, ago = fetch_cached_api_v3_site()
    # print(text)
    parsed = parse_api_v3_js(text)
    pprint(parsed)
    # {'currentPower': 588.72595,
    #  'lastDayEnergy': 1277.0,
    #  'lastUpdateTime': datetime.datetime(2022, 2, 6, 14, 8, tzinfo=<UTC>),
    #  'lifeTimeEnergy': 4351880.0}


if __name__ == '__main__':
    called_from_cli = (
        # Reading just JOURNAL_STREAM or INVOCATION_ID will not tell us
        # whether a user is looking at this, or whether output is passed to
        # systemd directly.
        any(os.isatty(i.fileno())
            for i in (sys.stdin, sys.stdout, sys.stderr)) or
        not os.environ.get('JOURNAL_STREAM'))
    sys.stdout.reconfigure(line_buffering=True)  # PYTHONUNBUFFERED, but better
    logging.basicConfig(
        level=(
            logging.DEBUG if os.environ.get('PE32SOLAREDGE_DEBUG', '')
            else logging.INFO),
        format=(
            '%(asctime)s %(message)s' if called_from_cli
            else '%(message)s'),
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S')

    if sys.argv[1:] == []:
        main()
    elif sys.argv[1:] == ['--publish']:
        fetch_and_publish()
    elif sys.argv[1:] == ['insert']:
        insert_latest_into_db()
    else:
        assert False
