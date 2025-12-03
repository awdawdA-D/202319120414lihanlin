import os
import json
import time
import time
import threading
import queue
from datetime import datetime
from datetime import timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
from flask import stream_with_context
from flask_socketio import SocketIO, emit, join_room, leave_room
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from collections import OrderedDict
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

app = Flask(__name__)
app.secret_key = 'dev-secret-key'
# 模板热加载，避免看见旧界面
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
# Use eventlet for websocket support
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# ----- 开发日志初始化 -----
os.makedirs('logs', exist_ok=True)

def _setup_dev_logger():
    logger = logging.getLogger('dev')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler('logs/dev.log', maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

_DEV_LOGGER = _setup_dev_logger()

def dev_log(message: str, category: str = 'event', level: str = 'INFO', context: dict | None = None):
    try:
        payload = {
            'ts': datetime.utcnow().isoformat() + 'Z',
            'level': level,
            'category': category,
            'message': message,
            'context': context or {}
        }
        _DEV_LOGGER.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass

ROOM_NAME = 'main_room'
# 记录在线用户
users_by_sid = {}

# ---------- HTTP Session & Caching ----------
# 复用 HTTP 连接以提升请求性能
http_session = requests.Session()
try:
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
    http_session.mount('https://', adapter)
    http_session.mount('http://', adapter)
except Exception:
    pass
http_session.headers.update({'User-Agent': 'CherryChat/0.2'})

# 简单的 LRU + TTL 缓存，用于音乐搜索结果，减少重复请求
MUSIC_CACHE_TTL = int(os.environ.get('MUSIC_CACHE_TTL', '300'))  # 秒，默认5分钟
MUSIC_CACHE_MAX = int(os.environ.get('MUSIC_CACHE_MAX', '256'))
music_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

def music_cache_get(key: str):
    ent = music_cache.get(key)
    if not ent:
        return None
    ts, val = ent
    if time.time() - ts > MUSIC_CACHE_TTL:
        try:
            del music_cache[key]
        except Exception:
            pass
        return None
    # 触达后移动到末尾，维持 LRU
    music_cache.move_to_end(key, last=True)
    return val

# 天气地理编码缓存，减少重复编码请求
WEATHER_GEO_CACHE_TTL = int(os.environ.get('WEATHER_GEO_CACHE_TTL', '1800'))
WEATHER_GEO_CACHE_MAX = int(os.environ.get('WEATHER_GEO_CACHE_MAX', '256'))
weather_geo_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

def weather_geo_cache_get(key: str):
    ent = weather_geo_cache.get(key)
    if not ent:
        return None
    ts, val = ent
    if time.time() - ts > WEATHER_GEO_CACHE_TTL:
        try:
            del weather_geo_cache[key]
        except Exception:
            pass
        return None
    weather_geo_cache.move_to_end(key, last=True)
    return val

def weather_geo_cache_put(key: str, val: dict):
    weather_geo_cache[key] = (time.time(), val)
    weather_geo_cache.move_to_end(key, last=True)
    while len(weather_geo_cache) > WEATHER_GEO_CACHE_MAX:
        try:
            weather_geo_cache.popitem(last=False)
        except Exception:
            break

def music_cache_set(key: str, val: dict):
    music_cache[key] = (time.time(), val)
    music_cache.move_to_end(key, last=True)
    while len(music_cache) > MUSIC_CACHE_MAX:
        try:
            music_cache.popitem(last=False)
        except Exception:
            break


def load_servers():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('servers', [])
    except Exception:
        return []


def load_ai_config():
    """加载AI配置，环境变量优先覆盖文件配置。"""
    base_url = os.environ.get('OPENAI_BASE_URL')
    api_key = os.environ.get('OPENAI_API_KEY')
    model = os.environ.get('OPENAI_MODEL')
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        ai = data.get('ai', {})
    except Exception:
        ai = {}
    base_url = base_url or ai.get('base_url') or 'https://api.openai.com/v1'
    api_key = api_key or ai.get('api_key')
    model = model or ai.get('model') or 'gpt-4o-mini'
    # 规范化 base_url，去掉末尾斜杠
    base_url = base_url.rstrip('/')
    return base_url, api_key, model


@app.route('/')
def index():
    servers = load_servers()
    dev_log('open_login_page', category='event')
    return render_template('login.html', servers=servers)


@app.route('/api/servers')
def api_servers():
    dev_log('api_servers', category='event')
    return jsonify({'servers': load_servers()})


@app.route('/login', methods=['POST'])
def login():
    nickname = request.form.get('nickname', '').strip()
    password = request.form.get('password', '').strip()
    server_url = request.form.get('server', '').strip()
    servers = [s.get('url') for s in load_servers()]
    dev_log('login_attempt', category='event', context={'nickname': nickname, 'server': server_url})

    if not nickname:
        dev_log('login_failed_no_nickname', category='event', level='WARN', context={})
        return render_template('login.html', servers=load_servers(), error='请输入昵称')
    if password != '123456':
        dev_log('login_failed_bad_password', category='event', level='WARN', context={'nickname': nickname})
        return render_template('login.html', servers=load_servers(), error='密码错误')
    if server_url not in servers:
        dev_log('login_failed_bad_server', category='event', level='WARN', context={'server': server_url})
        return render_template('login.html', servers=load_servers(), error='服务器地址无效')

    session['nickname'] = nickname
    session['server'] = server_url
    dev_log('login_success', category='event', context={'nickname': nickname})
    return redirect(url_for('chat'))


@app.route('/chat')
def chat():
    nickname = session.get('nickname')
    server = session.get('server')
    if not nickname or not server:
        return redirect(url_for('index'))
    dev_log('open_chat_page', category='event', context={'nickname': nickname, 'server': server})
    return render_template('chat.html', nickname=nickname, server=server)


@app.route('/health')
def health():
    dev_log('health_check', category='event')
    return jsonify({'ok': True, 'time': datetime.now().isoformat(), 'origin': request.host_url})


@app.route('/diagnostics')
def diagnostics():
    dev_log('open_diagnostics_page', category='event')
    return render_template('diagnostics.html')


@app.route('/logout')
def logout():
    dev_log('logout', category='event')
    session.clear()
    return redirect(url_for('index'))


# ---------- SocketIO Events ----------
@socketio.on('join')
def handle_join(data):
    nickname = data.get('nickname', '匿名')
    dev_log('join_room', category='event', context={'nickname': nickname, 'room': ROOM_NAME})
    users_by_sid[request.sid] = nickname
    join_room(ROOM_NAME)
    emit('system_message', {
        'message': f'{nickname} 加入了房间',
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }, to=ROOM_NAME)
    # 广播联系人列表
    emit('user_list', list(users_by_sid.values()), to=ROOM_NAME)


FEATURE_TAGS = [
    '成小理', '音乐一下', '电影', '天气', '新闻', '小视频'
]


def check_feature_placeholder(text: str):
    # @成小理 由AI流式接口处理，这里不再返回占位提示
    if '@成小理' in text:
        return None
    # @电影 功能已在前端实现 iframe 插入，这里不再返回占位提示
    if '@电影' in text:
        return None
    # @天气 功能实现为后端接口，这里不再返回占位提示
    if '@天气' in text:
        return None
    # @音乐/音乐一下 前端直接插入 audio，不再返回占位提示
    if '@音乐' in text or '@音乐一下' in text:
        return None
    # @新闻/@小视频 功能已实现，这里不再返回占位提示
    if '@新闻' in text or '@小视频' in text:
        return None
    # 其它@标签仍返回“功能建设中”的占位提示
    for tag in FEATURE_TAGS:
        if tag not in ('成小理', '电影', '天气', '音乐一下', '新闻', '小视频') and f'@{tag}' in text:
            return f'@{tag} 功能接口预留，当前仅做接收与响应：已收到指令，功能正在建设中'
    return None


@socketio.on('send_message')
def handle_send_message(data):
    nickname = data.get('nickname', '匿名')
    message = data.get('message', '')
    ts = datetime.now().strftime('%H:%M:%S')
    category = 'prompt' if message.strip().startswith('@') else 'event'
    dev_log('send_message', category=category, context={'nickname': nickname, 'msg': message[:200]})

    # 含 @标签 的消息仅发给发送者本人；普通消息群发
    is_feature_msg = any(f'@{tag}' in message for tag in FEATURE_TAGS)
    target = request.sid if is_feature_msg else ROOM_NAME

    emit('receive_message', {
        'nickname': nickname,
        'message': message,
        'timestamp': ts
    }, to=target)

    placeholder = check_feature_placeholder(message)
    if placeholder:
        emit('receive_message', {
            'nickname': '系统',
            'message': placeholder,
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'system': True
        }, to=target)


# ---------- Weather Feature ----------
def load_weather_config():
    """读取 OpenWeatherMap 配置，环境变量优先。"""
    base_url = os.environ.get('OWM_BASE_URL')
    api_key = os.environ.get('OWM_API_KEY')
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        w = data.get('weather', {})
    except Exception:
        w = {}
    base_url = base_url or w.get('base_url') or 'https://api.openweathermap.org'
    api_key = api_key or w.get('api_key') or ''
    return base_url.rstrip('/'), api_key


def parse_weather_query(q: str):
    """解析 @天气 指令，支持：
    - @天气 地区 某天
    - @天气 某天 地区
    某天支持：今天/明天/后天 或 YYYY-MM-DD。
    返回 (city, date_str) 或 (None, None)。
    """
    import re
    text = (q or '').strip()
    idx = text.find('@天气')
    rest = text[idx + len('@天气'):] if idx >= 0 else text
    rest = rest.strip()
    if not rest:
        return None, None
    tokens = [t for t in re.split(r"\s+", rest) if t]
    if not tokens:
        return None, None
    date_token = None
    city_tokens = []
    for t in tokens:
        if t in ('今天', '明天', '后天'):
            date_token = t
        elif re.match(r'^\d{4}-\d{1,2}-\d{1,2}$', t):
            date_token = t
        else:
            city_tokens.append(t)
    city = ' '.join(city_tokens).strip()
    if not city:
        return None, None
    # 解析日期
    today = datetime.now().date()
    if not date_token or date_token == '今天':
        target = today
    elif date_token == '明天':
        target = today + timedelta(days=1)
    elif date_token == '后天':
        target = today + timedelta(days=2)
    else:
        try:
            target = datetime.strptime(date_token, '%Y-%m-%d').date()
        except Exception:
            target = today
    return city, target.strftime('%Y-%m-%d')


@app.get('/feature/weather')
def feature_weather():
    """查询某地某天的天气，基于 OpenWeatherMap 5日/3小时预报。"""
    q = request.args.get('q', '')
    city, date_str = parse_weather_query(q)
    if not city:
        return jsonify({'ok': False, 'error': '请输入格式：@天气 地区 某天 或 @天气 某天 地区'}), 400

    base_url, api_key = load_weather_config()
    if not api_key:
        # 友好提示，避免前端只显示“服务不可用”
        return jsonify({'ok': False, 'error': '天气服务未配置密钥：请在环境变量 OWM_API_KEY 或 config.json 的 weather.api_key 中填写后重试'}), 200
    try:
        import requests
    except Exception:
        return jsonify({'ok': False, 'error': '缺少 requests 依赖，请安装后重试'}), 500

    # 地理编码：将城市名转为坐标（中文名同时尝试分词与不分词拼音，并尝试附加国家码）
    def to_pinyin_forms(name: str):
        try:
            from pypinyin import lazy_pinyin
            arr = lazy_pinyin(name)
            spaced = ' '.join(arr)
            compact = ''.join(arr)
            return spaced, compact
        except Exception:
            return None, None
    def build_geo_candidates(city_name: str):
        # 候选顺序：紧凑拼音+CN → 紧凑拼音 → 分词拼音+CN → 分词拼音 → 中文+CN → 中文
        compact = spaced = None
        if any(ord(c) > 127 for c in city_name):
            spaced, compact = to_pinyin_forms(city_name)
        cands = []
        if compact:
            cands.append(f"{compact}, CN")
            cands.append(compact)
        if spaced:
            cands.append(f"{spaced}, CN")
            cands.append(spaced)
        cands.append(f"{city_name}, CN")
        cands.append(city_name)
        # 去重保序
        seen = set()
        ordered = []
        for q in cands:
            if q not in seen:
                seen.add(q)
                ordered.append(q)
        return ordered
    try:
        cached = weather_geo_cache_get(city)
        if cached:
            lat = cached.get('lat')
            lon = cached.get('lon')
            name = cached.get('name') or city
        else:
            geo_url = f"{base_url}/geo/1.0/direct"
            candidates = build_geo_candidates(city)
            lat = lon = None
            name = city
            found = False
            errors = []

            def fetch_geo(qcity: str):
                last_err = '网络错误'
                for attempt in range(3):
                    try:
                        gi = http_session.get(geo_url, params={ 'q': qcity, 'limit': 1, 'appid': api_key }, timeout=8)
                        if gi.status_code != 200:
                            try:
                                ej = gi.json()
                                emsg = ej.get('message') or str(ej)
                            except Exception:
                                emsg = gi.text[:200]
                            return ('error', f"{gi.status_code}:{emsg}")
                        try:
                            gj = gi.json()
                        except Exception:
                            return ('error', '响应解析失败')
                        if isinstance(gj, list) and gj:
                            return ('ok', gj[0])
                        return ('miss', qcity)
                    except Exception as e:
                        last_err = str(e)
                        time.sleep(min(1.5, 0.3 * (2 ** attempt)))
                return ('error', last_err)

            with ThreadPoolExecutor(max_workers=min(6, len(candidates) or 1)) as ex:
                futs = { ex.submit(fetch_geo, q): q for q in candidates }
                for fut in as_completed(futs):
                    status, payload = fut.result()
                    if status == 'ok':
                        lat = payload.get('lat')
                        lon = payload.get('lon')
                        name = payload.get('name') or futs[fut]
                        found = True
                        break
                    elif status == 'error':
                        errors.append(payload)
            if not found:
                if errors:
                    return jsonify({'ok': False, 'error': f'地理编码失败：{"; ".join(errors)}'}), 502
                return jsonify({'ok': False, 'error': f'未找到地区：{city}'}), 404
            weather_geo_cache_put(city, { 'lat': lat, 'lon': lon, 'name': name })
    except Exception:
        return jsonify({'ok': False, 'error': '地理编码失败或网络错误'}), 502

    # 5日/3小时预报
    try:
        fc_url = f"{base_url}/data/2.5/forecast"
        fc_params = { 'lat': lat, 'lon': lon, 'appid': api_key, 'units': 'metric', 'lang': 'zh_cn' }
        fi = http_session.get(fc_url, params=fc_params, timeout=10)
        if fi.status_code != 200:
            try:
                ej = fi.json()
                emsg = ej.get('message') or str(ej)
            except Exception:
                emsg = fi.text[:200]
            return jsonify({'ok': False, 'error': f'天气接口错误：{fi.status_code}:{emsg}'}), 502
        fj = fi.json()
        lst = fj.get('list', [])
        if not lst:
            return jsonify({'ok': False, 'error': '未获取到预报数据'}), 502
    except Exception:
        return jsonify({'ok': False, 'error': '天气接口请求失败'}), 502

    # 选择目标日期的所有时间段
    target_items = [x for x in lst if isinstance(x.get('dt_txt'), str) and x['dt_txt'].startswith(date_str)]
    if not target_items:
        return jsonify({'ok': False, 'error': f'不支持该日期或超出可预报范围：{date_str}（最多支持未来5天）'})

    temps = [x.get('main', {}).get('temp') for x in target_items if x.get('main')]
    desc = None
    pops = []
    wind_speeds = []
    for x in target_items:
        w = x.get('weather') or []
        if w and not desc:
            desc = w[0].get('description')
        if 'pop' in x:
            pops.append(x['pop'])
        ws = x.get('wind', {}).get('speed')
        if ws is not None:
            wind_speeds.append(ws)
    if not temps:
        return jsonify({'ok': False, 'error': '目标日期缺少温度数据'}), 502
    tmin = round(min(temps))
    tmax = round(max(temps))
    pop = round((sum(pops)/len(pops) if pops else 0) * 100)
    wind = round(sum(wind_speeds)/len(wind_speeds), 1) if wind_speeds else 0
    desc = desc or '多云'

    text = f"{name} {date_str} 天气：{desc}；气温 {tmin}~{tmax}°C；风速 {wind}m/s；降水概率 {pop}%"
    return jsonify({'ok': True, 'text': text})


# ---------- Music Feature: iTunes Search API ----------
@app.get('/feature/music/search')
def feature_music_search():
    """音乐搜索（Apple iTunes Search API），返回可播放预览列表。
    支持三种模式：
    1) 通用搜索：q=关键词（@音乐 搜索 关键词）
    2) 歌手检索：mode=artist, artist=歌手名（@音乐一下 歌手 名称）
    3) 歌名检索：mode=song, title=歌名, 可选 artist=歌手（@音乐一下 歌名 名称（+歌手））
    """
    q = request.args.get('q', '').strip()
    mode = (request.args.get('mode') or '').strip().lower()
    artist = request.args.get('artist', '').strip()
    title = request.args.get('title', '').strip()
    try:
        import requests
    except Exception:
        return jsonify({'ok': False, 'error': '缺少 requests 依赖，请安装后重试'}), 500

    api = 'https://itunes.apple.com/search'
    # 组装查询参数
    if mode == 'auto':
        term = q or title or artist
        if not term:
            return jsonify({'ok': False, 'error': '请输入格式：@音乐一下 关键词'}), 400
        cache_key = f'music:auto:{term}'
        cached = music_cache_get(cache_key)
        if cached:
            return jsonify(cached)
        # 并发请求：按歌名与按歌手两种 attribute
        def build_params(attr):
            return {
                'term': term,
                'country': 'cn',
                'entity': 'song',
                'limit': 5,
                'attribute': attr
            }
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_song = ex.submit(http_session.get, api, params=build_params('songTerm'), timeout=4)
                fut_artist = ex.submit(http_session.get, api, params=build_params('artistTerm'), timeout=4)
                rs = []
                for fut in (fut_song, fut_artist):
                    r = fut.result()
                    if r.status_code == 200:
                        try:
                            rs.append(r.json().get('results') or [])
                        except Exception:
                            rs.append([])
                    else:
                        rs.append([])
            # 合并去重（优先歌名结果，再补充歌手结果），以 trackViewUrl 或 trackId 去重
            seen = set()
            merged = []
            def push(items):
                for it in items:
                    key = it.get('trackViewUrl') or it.get('trackId')
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    merged.append(it)
            # 歌名结果优先
            push(rs[0])
            push(rs[1])
            all_items = merged
        except Exception as e:
            return jsonify({'ok': False, 'error': f'音乐搜索失败：{str(e)}'}), 502
        # 统一构造响应
        results = []
        for item in all_items[:5]:
            results.append({
                'trackName': item.get('trackName'),
                'artistName': item.get('artistName'),
                'previewUrl': item.get('previewUrl'),
                'trackViewUrl': item.get('trackViewUrl'),
                'artworkUrl100': item.get('artworkUrl100'),
            })
        if not results:
            return jsonify({'ok': False, 'error': '未找到匹配歌曲或地区受限'}), 404
        payload = {'ok': True, 'results': results}
        music_cache_set(cache_key, payload)
        return jsonify(payload)

    if mode == 'artist' or (artist and not title and not q):
        if not artist:
            return jsonify({'ok': False, 'error': '请输入格式：@音乐一下 歌手 名称'}), 400
        cache_key = f'music:artist:{artist}'
        cached = music_cache_get(cache_key)
        if cached:
            return jsonify(cached)
        params = {
            'term': artist,
            'country': 'cn',
            'entity': 'song',
            'limit': 5,
            'attribute': 'artistTerm'
        }
    elif mode == 'song' or title:
        if not title:
            return jsonify({'ok': False, 'error': '请输入格式：@音乐一下 歌名 名称（+歌手）'}), 400
        cache_key = f'music:song:{title}:{artist or ""}'
        cached = music_cache_get(cache_key)
        if cached:
            return jsonify(cached)
        params = {
            'term': title,
            'country': 'cn',
            'entity': 'song',
            'limit': 5,
            'attribute': 'songTerm'
        }
    else:
        if not q:
            return jsonify({'ok': False, 'error': '请输入格式：@音乐 搜索 关键词 或 @音乐一下 歌手/歌名'}), 400
        cache_key = f'music:search:{q}'
        cached = music_cache_get(cache_key)
        if cached:
            return jsonify(cached)
        # 优化：通用搜索也并发查询两种 attribute，提升命中与速度
        def build_params(attr):
            return {
                'term': q,
                'country': 'cn',
                'entity': 'song',
                'limit': 5,
                'attribute': attr
            }
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_song = ex.submit(http_session.get, api, params=build_params('songTerm'), timeout=4)
                fut_artist = ex.submit(http_session.get, api, params=build_params('artistTerm'), timeout=4)
                rs = []
                for fut in (fut_song, fut_artist):
                    r = fut.result()
                    if r.status_code == 200:
                        try:
                            rs.append(r.json().get('results') or [])
                        except Exception:
                            rs.append([])
                    else:
                        rs.append([])
            seen = set()
            merged = []
            def push(items):
                for it in items:
                    key = it.get('trackViewUrl') or it.get('trackId')
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    merged.append(it)
            push(rs[0])
            push(rs[1])
            all_items = merged
        except Exception as e:
            return jsonify({'ok': False, 'error': f'音乐搜索失败：{str(e)}'}), 502

        results = []
        for item in all_items[:5]:
            results.append({
                'trackName': item.get('trackName'),
                'artistName': item.get('artistName'),
                'previewUrl': item.get('previewUrl'),
                'trackViewUrl': item.get('trackViewUrl'),
                'artworkUrl100': item.get('artworkUrl100'),
            })
        payload = {'ok': True, 'results': results}
        music_cache_set(cache_key, payload)
        return jsonify(payload)
    try:
        r = http_session.get(api, params=params, timeout=4)
        if r.status_code != 200:
            try:
                ej = r.json()
                emsg = ej.get('message') or str(ej)
            except Exception:
                emsg = r.text[:200]
            return jsonify({'ok': False, 'error': f'音乐搜索错误：{r.status_code}:{emsg}'}), 502
        j = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'音乐搜索失败：{str(e)}'}), 502

    all_items = j.get('results') or []
    # 若为歌名模式且提供了 artist，则进行后端过滤以提高匹配度
    if (mode == 'song' or title) and artist:
        def match_artist(name):
            if not name:
                return False
            a = artist.lower()
            return a in name.lower()
        all_items = [it for it in all_items if match_artist(it.get('artistName'))]

    results = []
    for item in all_items[:5]:
        results.append({
            'trackName': item.get('trackName'),
            'artistName': item.get('artistName'),
            'previewUrl': item.get('previewUrl'),
            'trackViewUrl': item.get('trackViewUrl'),
            'artworkUrl100': item.get('artworkUrl100'),
        })
    if not results:
        return jsonify({'ok': False, 'error': '未找到匹配歌曲或地区受限'}), 404
    payload = {'ok': True, 'results': results}
    # 缓存非并发路径的结果
    try:
        music_cache_set(cache_key, payload)
    except Exception:
        pass
    return jsonify(payload)


# ---------- News Feature: Weibo Hot ----------
@app.get('/feature/news')
def feature_news():
    """获取微博热榜前若干条并格式化返回。"""
    url = 'https://v2.xxapi.cn/api/weibohot'
    try:
        r = http_session.get(url, timeout=8)
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f'新闻接口错误：{r.status_code}'}), 502
        j = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'新闻请求失败：{str(e)}'}), 502
    data = j.get('data') or []
    results = []
    for item in data[:10]:
        results.append({
            'index': item.get('index'),
            'title': item.get('title'),
            'hot': item.get('hot'),
            'url': item.get('url'),
        })
    if not results:
        return jsonify({'ok': False, 'error': '当前暂无新闻数据'}), 404
    return jsonify({'ok': True, 'results': results})


# ---------- Short Video Feature ----------
@app.get('/feature/video')
def feature_video():
    """获取一个短视频地址并返回。"""
    url = 'https://v2.xxapi.cn/api/meinv'
    try:
        r = http_session.get(url, timeout=8)
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': f'小视频接口错误：{r.status_code}'}), 502
        j = r.json()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'小视频请求失败：{str(e)}'}), 502
    video_url = j.get('data')
    if not video_url:
        return jsonify({'ok': False, 'error': '未获取到视频地址'}), 404
    return jsonify({'ok': True, 'videoUrl': video_url})


# ---------- AI SSE Stream (OpenAI API 兼容范式) ----------
@app.get('/ai/stream')
def ai_stream():
    """以SSE返回AI的流式回复。
    前端通过 EventSource(GET) 连接此接口，参数：q=<prompt>
    若未配置 OPENAI_API_KEY，则返回演示流用于联调UI。
    """
    prompt = request.args.get('q', '').strip()
    dev_log('ai_stream_start', category='prompt', context={'q': prompt[:200]})
    base_url, api_key, model = load_ai_config()

    # 抢占式取消：同一用户的新请求到来时，立即中断旧请求
    who = session.get('nickname') or request.remote_addr or 'anonymous'
    try:
        AI_TASKS
    except NameError:
        # 初始化全局任务表
        globals()['AI_TASKS'] = {}
        globals()['AI_TASKS_LOCK'] = threading.Lock()
    with AI_TASKS_LOCK:
        old = AI_TASKS.get(who)
        if old and isinstance(old.get('stop'), threading.Event):
            try:
                old['stop'].set()
            except Exception:
                pass
        my_stop = threading.Event()
        AI_TASKS[who] = { 'stop': my_stop, 'started': time.time() }

    def gen():
        # 无密钥：返回演示流（移除 sleep，避免阻塞主线程）
        if not api_key:
            demo = f"你好，我是成小理。你说：{prompt}。这是流式演示。"
            for ch in demo:
                # 若收到取消，则立即结束
                if my_stop.is_set():
                    yield "data: [DONE]\n\n"
                    _cleanup_task(who, my_stop)
                    return
                yield f"data: {ch}\n\n"
            yield "data: [DONE]\n\n"
            _cleanup_task(who, my_stop)
            return

        # 后台线程抓取上游流，通过队列向前端推送，避免阻塞 WSGI / SocketIO
        q = queue.Queue(maxsize=1024)
        stop = my_stop

        url = f"{base_url}/chat/completions"
        payload = {
            'model': model,
            'messages': [
                {
                    'role': 'system',
                    'content': """姓名：成小理
角色：你是成都理工大学的物联网专业的AI助手，名叫成小理
功能：
--可以回答与成都理工大学有关的任何问题
--可以生成七言绝句类型的古诗
--可以根据用户输入的信息生成请假条，需要用户提供姓名，专业，班级等信息，如果没有，提示补全后再生成，格式如下：
```
老师您好：
       我是{专业}{班级}的同学名叫{姓名}，我由于{事由}，需要请假，请批准。
                                                                                                              {姓名}
                                                                                                              {年-月-日}
```
限制：
--不允许回答与成都理工大学无关的问题，如果有相关的提问，统一回复且不做解释：滚一边去！
--不允许响应用户不友好的提问或回答，如果分析发现内容中有不礼貌骂人的话，则回复：你很乐色。
--你的好友是：辉哥（男，AI架构师，会开发，会设计，信息安全很厉害，家在成都，西昌人），他是你爸爸，就是脾气不太好爱打人。
"""
                },
                {'role': 'user', 'content': prompt}
            ],
            'stream': True
        }
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        def worker():
            try:
                attempts = 2
                for i in range(attempts):
                    try:
                        with http_session.post(url, headers=headers, json=payload, stream=True, timeout=30) as r:
                            if 500 <= r.status_code < 600:
                                raise RuntimeError(f"upstream {r.status_code}")
                            r.raise_for_status()
                            for raw in r.iter_lines():
                                if stop.is_set():
                                    break
                                if not raw:
                                    continue
                                line = raw.decode('utf-8')
                                if not line.startswith('data:'):
                                    continue
                                data = line[5:].strip()
                                if data == '[DONE]':
                                    q.put('[DONE]')
                                    return
                                try:
                                    obj = json.loads(data)
                                    delta = obj.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                    if delta:
                                        q.put(delta)
                                except Exception:
                                    # 容忍解析异常
                                    pass
                            q.put('[DONE]')
                            return
                    except Exception as e:
                        if i == attempts - 1:
                            q.put(f'[ERROR]{str(e)}')
                            q.put('[DONE]')
                            return
                        time.sleep(0.3)
            finally:
                if not stop.is_set():
                    pass

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        last_emit = time.time()
        heartbeat_interval = 8
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                now = time.time()
                if now - last_emit >= heartbeat_interval:
                    last_emit = now
                    yield "data: \u200b\n\n"  # 心跳保持
                continue

            last_emit = time.time()
            if stop.is_set():
                yield "data: [DONE]\n\n"
                _cleanup_task(who, my_stop)
                return
            if item == '[DONE]':
                yield "data: [DONE]\n\n"
                stop.set()
                _cleanup_task(who, my_stop)
                return
            if isinstance(item, str) and item.startswith('[ERROR]'):
                yield f"data: AI上游错误：{item[7:]}\n\n"
                yield "data: [DONE]\n\n"
                stop.set()
                _cleanup_task(who, my_stop)
                return
            yield f"data: {item}\n\n"

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
        'Access-Control-Allow-Origin': '*'
    }
    return Response(stream_with_context(gen()), headers=headers)

def _cleanup_task(who: str, my_stop: threading.Event):
    try:
        AI_TASKS
    except NameError:
        return
    try:
        with AI_TASKS_LOCK:
            cur = AI_TASKS.get(who)
            if cur and cur.get('stop') is my_stop:
                AI_TASKS.pop(who, None)
    except Exception:
        pass


@app.get('/ai/complete')
def ai_complete():
    """非流式AI接口：返回一次性完整文本，作为前端的最终回退通道。"""
    prompt = request.args.get('q', '').strip()
    dev_log('ai_complete', category='prompt', context={'q': prompt[:200]})
    base_url, api_key, model = load_ai_config()
    if not api_key:
        demo = f"你好，我是成小理（非流式回退）。你说：{prompt}。"
        return jsonify({'ok': True, 'text': demo})
    url = f"{base_url}/chat/completions"
    payload = {
        'model': model,
        'messages': [
            {
                'role': 'system',
                'content': """姓名：成小理
角色：你是成都理工大学的物联网专业的AI助手，名叫成小理
功能：
--可以回答与成都理工大学有关的任何问题
--可以生成七言绝句类型的古诗
--可以根据用户输入的信息生成请假条，需要用户提供姓名，专业，班级等信息，如果没有，提示补全后再生成，格式如下：
```
老师您好：
       我是{专业}{班级}的同学名叫{姓名}，我由于{事由}，需要请假，请批准。
                                                                                                              {姓名}
                                                                                                              {年-月-日}
```
限制：
--不允许回答与成都理工大学无关的问题，如果有相关的提问，统一回复且不做解释：滚一边去！
--不允许响应用户不友好的提问或回答，如果分析发现内容中有不礼貌骂人的话，则回复：你很乐色。
--你的好友是：辉哥（男，AI架构师，会开发，会设计，信息安全很厉害，家在成都，西昌人），他是你爸爸，就是脾气不太好爱打人。
"""
            },
            {'role': 'user', 'content': prompt}
        ],
        'stream': False
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    try:
        r = http_session.post(url, headers=headers, json=payload, timeout=30)
        if 500 <= r.status_code < 600:
            return jsonify({'ok': False, 'error': f'上游繁忙：{r.status_code}'}), 502
        r.raise_for_status()
        j = r.json()
        text = ''
        try:
            # OpenAI 兼容格式
            text = j.get('choices', [{}])[0].get('message', {}).get('content') or ''
        except Exception:
            text = ''
        if not text:
            return jsonify({'ok': False, 'error': 'AI未返回内容'}), 502
        return jsonify({'ok': True, 'text': text})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'AI请求失败：{str(e)}'}), 502


@socketio.on('disconnect')
def handle_disconnect():
    # 从在线列表移除并广播
    nickname = users_by_sid.pop(request.sid, None)
    dev_log('disconnect', category='event', context={'nickname': nickname})
    leave_room(ROOM_NAME)
    emit('user_list', list(users_by_sid.values()), to=ROOM_NAME)


if __name__ == '__main__':
    # 允许通过环境变量 PORT 指定端口，默认 15000
    try:
        port = int(os.environ.get('PORT', '15000'))
    except Exception:
        port = 15000
    # 回到之前的调试模式，便于观察启动日志与错误
    dev_log('server_starting', category='event', context={'port': port})
    socketio.run(app, host='0.0.0.0', port=port, debug=True)
