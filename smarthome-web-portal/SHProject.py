# -*- coding: utf-8 -*-
"""
app view
"""
import os
import json
import logging
import hashlib
import functools
import time
import datetime
from flask import redirect, url_for, abort
from flask import request, session, make_response
from flask import Flask, render_template, jsonify
from flask.ext.socketio import SocketIO, emit, disconnect
from decimal import Decimal
from utils import logsettings
from utils import util
from utils.config import config
from DB.api import resource, user, gateway, sensor_group, actual_weather, actual_power, his_weather, predicted_power
from RestClient.sensor import Sensor
from RestClient.api import ApiClient
from RestClient.api.iotError import IoTRequestError
from utils.settings import SECRET_KEY, ALERT_GRP, STATUS_GRP, DATA_GRP, BRILLO_GRP, TAP_ENV_VARS
from datetime import timedelta


try:
    import pymysql
    pymysql.install_as_MySQLdb()
except ImportError:
    pass

""" static resource location"""
static_url_prefix = ''
app = Flask(__name__, static_url_path='')

app.config['SECRET_KEY'] = SECRET_KEY

logger = logging.getLogger(__name__)
socketio = SocketIO(app, pingTimeout=60)


def login_required(func):
    """
    Decorator to check whether user's login
    If not, redirect to login page
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if 'username' in session:
            return func(*args, **kwargs)
        else:
            return redirect(url_for('login'))
    return wrapper


def authenticated_only(f):
    """
    Check login for web socket connections
    """
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if 'username' not in session:
            disconnect()
        else:
            return f(*args, **kwargs)
    return wrapped


@socketio.on('my event', namespace='/index')
def test_message(message):
    emit('my response', {'data': message['data']})


def _get_historical_data(sensor, start_time, end_time):
    try:
        get_data = util.get_class("DB.api.{}.get_data_by_time".format(sensor))
        dic = get_data(start_time, end_time)
        # print dic
        return [] if len(dic) == 0 else dic
    except ImportError:
        abort(400)


@socketio.on('my data', namespace='/index')
def get_historical_data(message):
    """
    Get average sensor data in a period of time
    """
    sensor = message['data']
    date_range = message['date']
    print sensor, date_range
    start_time = date_range[0]
    end_time = date_range[1]
    data = _get_historical_data(sensor, start_time, end_time)
    emit('my ' + sensor, {'data': data})


@socketio.on('my broadcast event', namespace='/index')
def test_message(message):
    emit('my response', {'data': message['data']}, broadcast=True)


@socketio.on('connect', namespace='/index')
@authenticated_only
def test_connect():
    logger.info('Connected.')
    emit('my response', {'data': 'Connected'})


@socketio.on('disconnect', namespace='/index')
def test_disconnect():
    #disconnect()
    logger.info('Client disconnected')


@socketio.on_error('/index')  # handles the '/index' namespace
def error_handler_chat(e):
    print e


def _compose_sensor_data(sensor_type, latest_data, record_key, result_key, result):
    """
    :param sensor_type: the sensor type
    :param latest_data: the latest sensor data record
    :param record_key: the keyword stores the data in table
    :param result_key: the key name in the return data dict
    :param result: the return data dict
    :return:
    """

    val_dict = {
        'uuid': latest_data.get('resource').get('uuid') if latest_data.get('resource') else None,
        'resource_id': latest_data.get('resource').get('id') if latest_data.get('resource')
        else latest_data.get('resource_id'),
        'path': latest_data.get('resource').get('path') if latest_data.get('resource') else None,
        'tag': latest_data.get('resource').get('tag') if latest_data.get('resource') else None,
    }

    # extract group background color
    res_obj = latest_data.get('resource')
    color = None
    if res_obj:
        # print latest_data.get('resource').get('sensor_group')
        if res_obj.get('sensor_group_id'):
            color = sensor_group.get_sensor_group(res_obj.get('sensor_group_id'))
            # use the default color if null
        color = {'color': '#fff', 'name': ''} if not color else color
        val_dict.update(
            {'color': color}
        )

    # decouple the tag field for environmental sensors
    if latest_data.get('resource') and latest_data.get('resource').get('tag'):
        # a pure digit string will be treated as json
        tag = latest_data.get('resource').get('tag')
        if util.is_json(tag) and not tag.strip().lstrip('-').isdigit():
            tag_dict = json.loads(tag)
            val_dict.update({
                "tag": tag_dict.get(sensor_type),
            })

    if isinstance(record_key, dict):
        val_dict.update({
            'value': record_key.get('value'),
        })
    elif isinstance(record_key, list):
        for key in record_key:
            val = latest_data.get(key)
            val_dict.update({
                key: str(val) if isinstance(val, (float, Decimal)) else val,
            })
    elif latest_data.get(record_key) is not None:
        val = latest_data.get(record_key)
        val_dict.update({
            'value': str(val) if isinstance(val, (float, Decimal)) else val,
        })
    else:
        val_dict.update({
            'value': "",
        })

    if result_key == "brillo":
        uuid = latest_data.get('resource').get('uuid')
        if result[result_key].get(uuid) is None:
            result[result_key].update({uuid: {sensor_type: val_dict}})
        else:
            result[result_key][uuid].update({sensor_type: val_dict})
    else:
        if result[result_key].get(sensor_type) is None:
            result[result_key].update({sensor_type: [val_dict, ]})
        else:
            result[result_key][sensor_type].append(val_dict)


def _get_sensor_data(token_dict):
    res = resource.list_resource(status=1, gateway_id=session['gateway_id'])
    default_token = util.format_datetime(util.get_utc_now() - datetime.timedelta(minutes=1))
    ret = {
        'alert': {},
        'status': {},
        'data': {},
        'brillo': {},
    }
    for sensor in res:
        typ = sensor.get('sensor_type').get('mapping_class')
        href = sensor.get('path')
        resource_id = sensor.get("id")
        if href.startswith("/a/"):
            if typ in ALERT_GRP:
                token = token_dict.get(str(resource_id)) if str(resource_id) in token_dict.keys() \
                                                            and token_dict.get(str(resource_id)) else default_token
                latest_data = util.get_class("DB.api.{}.get_latest_alert_by_gateway_uuid"
                                             .format(typ))(resource_id=resource_id,
                                                           token=token)
                latest_data = latest_data if latest_data else {"resource_id": resource_id}
            elif typ == 'power':
                latest_data = util.get_class("DB.api.energy.get_latest_by_gateway_uuid".format(typ))(resource_id=resource_id)
            else:
                latest_data = util.get_class("DB.api.{}.get_latest_by_gateway_uuid".format(typ))(resource_id=resource_id)
            if latest_data is None:
                continue
            # print latest_data
            if typ in ALERT_GRP:
                _compose_sensor_data(typ, latest_data, 'created_at', 'alert', ret)
            elif typ in STATUS_GRP:
                if typ == "rgbled":
                    val = True if latest_data.get('rgbvalue') == "[255, 0, 0]" else False
                    _compose_sensor_data(typ, latest_data, {'value': val}, 'status', ret)
                else:
                    _compose_sensor_data(typ, latest_data, 'status', 'status', ret)
            elif typ in DATA_GRP:
                # extract values from the db query result
                if typ in ['temperature', 'illuminance']:
                    key_words = [typ]
                elif typ == 'solar':
                    key_words = ['tiltpercentage']
                elif typ == 'power':
                    key_words = ['value']
                elif typ == 'environment':
                    key_words = ['temperature', 'humidity', 'pressure', 'uv_index']

                for key in key_words:
                    sensor_type = typ if typ != "environment" else key
                    _compose_sensor_data(sensor_type, latest_data, key, 'data', ret)
        elif href.startswith("/brillo/"):
            latest_data = util.get_class("DB.api.{}.get_latest_by_gateway_uuid".format(typ))(
                resource_id=resource_id)
            # print latest_data
            if latest_data is None:
                continue
            if typ in BRILLO_GRP:
                if typ in ['brightness']:
                    keys = [typ]
                elif typ == 'rgbled':
                    keys = ['rgbvalue']
                elif typ == 'audio':
                    keys = ['volume', 'mute']
                elif typ == 'mp3player':
                    keys = ['media_states', 'playlist', 'state', 'title']

                _compose_sensor_data(typ, latest_data, keys, 'brillo', ret)
    return ret


@app.route('/get_sensor')
@login_required
def get_sensor():
    """
    Get sensor data by token
    """
    token_header = request.headers.get('token')
    token_dict = json.loads(token_header) if token_header else dict()
    ret = _get_sensor_data(token_dict)
    return jsonify(data=ret), 201


def _compose_sensor_tag(data):
    if "tag" in data["value"]:
        res = resource.get_resource(id=data["resource_id"])
        sensor_type = res.get("sensor_type").get("mapping_class")

        if sensor_type == "environment":
            new_tag = data["value"]['tag']
            tag = res.get("tag")
            sensor_type = data["type"]
            if sensor_type:
                sensor_type = sensor_type.replace(" ", "_")
            try:
                tag_dict = json.loads(tag)
            except:
                tag_dict = {}
            tag_dict.update({
                sensor_type: new_tag
            })
            data["value"].update({
                "tag": json.dumps(tag_dict)
            })


@app.route('/update_sensor_attr', methods=['POST'])
@login_required
def update_sensor_attr():
    """
    Update sensor properties
    :return: uuid and status code
    """
    data = request.json
    print data
    if "resource_id" not in data.keys() \
            or "value" not in data.keys() \
            or not data.get("resource_id") \
            or not data.get("value") \
            or not isinstance(data.get("value"), dict):
        abort(400)
    _compose_sensor_tag(data)
    updated_res = resource.update_resource(id=data['resource_id'], **data["value"])
    return jsonify(resource_id=updated_res.get("id")), 200


@app.route('/get_geo_location')
@login_required
def get_geo_location():
    """Get the Geolocation of the current account"""
    location = gateway.get_geo(gateway_id=session.get('gateway_id'))
    return jsonify({'geo': location}), 201


@app.route('/update_sensor', methods=['PUT'])
@login_required
def update_sensor():
    """
    update sensor status
    Http PUT: {
                'resource_id': '5',
                'data': { 'value': false}
                }
    """
    content = request.get_json(silent=True)
    resource_id = content.get('resource_id')
    data = content.get('data')
    if not resource_id or not isinstance(resource_id, int):
        abort(400)
    try:
        res = resource.get_resource(id=resource_id)
    except:
        abort(404)
    print "content: " + str(content)
    sensor = Sensor(uuid=res.get('uuid'), path=res.get('path'),
                    resource_type=res.get('sensor_type').get('type'), username=session.get('username'))
    try:
        sts = sensor.update_status(data)
        return jsonify({'status': sts}), 201
    except IoTRequestError:
        abort(500)


@app.route('/add_sensor_group', methods=['POST'])
@login_required
def add_sensor_group():
    """
    update sensor group
    Http POST: {
                'gateway_id': 1,
                'color': '#fff',
                'name': 'name',
                }
    """
    content = request.get_json(silent=True)
    gateway_id = content.get('gateway_id')
    if not gateway_id or not content.get('color') or not content.get('name'):
        abort(400)
    if not isinstance(gateway_id, int):
        if not isinstance(gateway_id, str):
            abort(400)
        elif not gateway_id.isdigit():
            abort(400)
    try:
        return jsonify(sensor_group.new(content)), 201
    except:
        abort(500)


@app.route('/')
def root():
    return redirect(url_for('login'), code=302)


@app.route('/login')
def login():
    return render_template('login.html', 
                           static_url_prefix=static_url_prefix)


@app.route('/index')
@login_required
def index():
    return render_template('home_dashboard.html',
                           static_url_prefix=static_url_prefix)


@app.route('/energy')
def energy():
    return render_template('energy_dashboard.html',
                           static_url_prefix=static_url_prefix)


def _get_cf_instance_number():
    num = ''
    endpoint = config.get_tap_auth_endpoint()
    api = config.get_tap_api_endpoint()
    if endpoint and api:
        proxy = config.get_all_proxy()
        client = ApiClient(endpoint, proxy)
        client.add_header('content-type', 'application/x-www-form-urlencoded;charset=utf-8')
        client.add_header('accept', 'application/json;charset=utf-8')
        client.add_header('authorization', 'Basic Y2Y6')
        data = {
                    'grant_type': 'password',
                    'username': config.get_tap_uname(),
                    'password': config.get_tap_pwd(),
                }
        ret = client.post('/oauth/token', data)
        if ret.ok():
            at = ret.content.get('access_token')
            cc = ApiClient(api, proxy)
            cc.add_header('authorization', 'bearer ' + at)
            resp = cc.get('/v2/apps?q=name:{}'.format(config.get_tap_app_name()))
            if resp.ok():
                num = resp.content['resources'][0]['entity']['instances']
        else:
            print "response: " + str(ret.content)
            print "status code:" + str(ret.status_code)
            print "post data:" + str(data)
    return str(num)


@app.route('/cf_instance')
@login_required
def get_instance():
    """
    Get instance ID and total running instances in the CF Cloud
    Return data in json format
    """
    inst = {}
    env_str = os.getenv("VCAP_APPLICATION", "")
    if env_str:
        inst['Instance'] = os.getenv('CF_INSTANCE_INDEX', '')
        inst['Total'] = _get_cf_instance_number()

        env_dict = json.loads(env_str)
        for key in sorted(env_dict.keys()):
            if str(key).upper() in TAP_ENV_VARS:
                inst[key] = env_dict[key]
    return jsonify({'cf_instance': inst}), 201


@app.route('/get_groups')
@login_required
def list_sensor_groups():
    """
    Get the sensor groups of the current gateway
    Return data in json format
    """
    gateway_id = session['gateway_id']
    sg = sensor_group.get_all_groups(gateway_id=gateway_id)
    return jsonify(sensor_groups=sg)


@app.route('/get_gateways')
def list_gateways():
    # todo: need some security mechanism to protect the data
    gateways = gateway.list_gateways(status=True)
    keyword = config.get_map_keyword()
    types = config.get_map_types()
    return jsonify({'gateways': gateways,
                    'keyword': keyword,
                    'types': types}), 201


@app.route('/authenticate', methods=['POST'])
def authenticate():
    username = request.form['username']
    password = request.form['password']
    password_sha = hashlib.sha256(password).hexdigest()
    if password_sha == user.login(username):
        gateway_id = user.user_gatewayid(username)
        # url = user.user_url(username)
        session['gateway_id'] = str(gateway_id)
        session['username'] = username
        info = 'Welcome ' + username
        print info
        resp = make_response(redirect(url_for('index'), code=302))
        resp.set_cookie('JSESSIONID', 'Sticky session.')
        resp.set_cookie('gateway_id', str(gateway_id))
        return resp
    else:
        info = 'Username or password are incorrect'
        return render_template('login.html', info=info,
                               static_url_prefix=static_url_prefix)


@app.route('/logout')
def logout():
    session.pop('gateway_id', None)
    session.pop('username', None)
    resp = make_response(redirect(url_for('login'), code=302))
    resp.set_cookie('__VCAP_ID__', '', expires=0)
    return resp


@app.route('/temp_actual')
@login_required
def temp_actual():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "######temp_actual"
    print today_date_str

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	
    startDate = today_date - timedelta(days=3)
    endDate = today_date + timedelta(days=3)
    print startDate
    print endDate
    his_temper_info = his_weather.get_weather_by_date(startDate, endDate, order_by=[('publish_date', False)])
    print his_temper_info
    for info in his_temper_info:
        info['publish_date']=info['publish_date'].strftime('%Y-%m-%d')
    json_str=json.dumps(his_temper_info)
    return json_str, 200


@app.route('/temp_future')
@login_required
def temp_future():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "######temp_future"

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	

    startDate = today_date - timedelta(days=3) 
    endDate = today_date + timedelta(days=3)
    print startDate
    acutal_weather_info = actual_weather.get_weather_by_date(today_date, today_date, region_id=1, order_by=[('publish_date', False)])
    print acutal_weather_info
    for info in acutal_weather_info:
        info['publish_date'] = info['publish_date'].strftime('%Y-%m-%d')
        info['forecast_date'] = info['forecast_date'].strftime('%Y-%m-%d')
    json_str = json.dumps(acutal_weather_info)
    return json_str, 200


@app.route('/temp_today')
@login_required
def temp_today():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "######temp_today"

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	

    startDate = today_date - timedelta(days=3) 
    endDate = today_date + timedelta(days=3)
    print startDate
    acutal_weather_info = actual_weather.get_weather_by_date(today_date, today_date, order=0, region_id=1, order_by=[('publish_date', False)])
    print acutal_weather_info
    for info in acutal_weather_info:
        info['publish_date']=info['publish_date'].strftime('%Y-%m-%d')
        info['forecast_date']=info['forecast_date'].strftime('%Y-%m-%d')
    json_str=json.dumps(acutal_weather_info)
    return json_str, 200


@app.route('/power_actual')
@login_required
def power_actual():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "######power_actual"

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	
    startDate = today_date - timedelta(days=3) 
    endDate = today_date + timedelta(days=3)
    print startDate
    actual_power_info = actual_power.get_power_by_date(startDate, endDate, region_id=1, order_by=[('collect_date', False)])
    for info in actual_power_info:
        info['collect_date']=info['collect_date'].strftime('%Y-%m-%d')
    json_str=json.dumps(actual_power_info)
    print actual_power_info
    return json_str, 200


@app.route('/power_predict_his')
@login_required
def power_predict_his():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "#########power_predict_his"

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	

    startDate = today_date - timedelta(days=3)
    endDate = today_date + timedelta(days=3)

    predict_power_info = predicted_power.get_power_by_date(startDate,endDate, order=0, region_id=1, order_by=[('publish_date', False)])
    for info in predict_power_info:
        info['publish_date'] = info['publish_date'].strftime('%Y-%m-%d')
    json_str = json.dumps(predict_power_info)
    print predict_power_info
    return json_str, 200


@app.route('/power_future')
@login_required
def power_future():
    region = request.args.get('region')
    today_date_str = request.args.get('today_date')
    print "#########power_future"

    today_date = datetime.datetime.strptime(today_date_str, "%m/%d/%Y").date()	

    #startDate = today_date - timedelta(days=3) 
    #endDate = today_date + timedelta(days=3)

    predict_power_info = predicted_power.get_power(publish_date=today_date, order_by=[('publish_date', False)])
    for info in predict_power_info:
        info['publish_date']=info['publish_date'].strftime('%Y-%m-%d')
    json_str=json.dumps(predict_power_info)
    print predict_power_info
    return json_str, 200

#@app.route('/get_power')
#def get_power():


if __name__ == '__main__':
    logger = logsettings.setup_log()
    logger.info('init SMART HOME project ...')
    port = os.getenv('PORT', '3000')
    socketio.run(app, debug=False, port=int(port), host="0.0.0.0")

