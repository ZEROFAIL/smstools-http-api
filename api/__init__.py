#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import subprocess
import tempfile
import time
from base64 import b64encode

import filelock
import serial
from werkzeug.security import check_password_hash

from flask import Flask
from flask import jsonify
from flask import make_response
from flask import request
from flask.ext.httpauth import HTTPBasicAuth

CSQ_REGEX = re.compile(r'\+CSQ: (\d{1,2}),')
CREG_REGEX = re.compile(r'\+CREG: (\d),\d')
SERIAL_DEVICE = 'ttyUSB3'
SERIAL_DEVICE_PATH = '/dev/{}'.format(SERIAL_DEVICE)
SERIAL_DEVICE_RESET_PATH = '/dev/ttyUSB2'
SLOCK = filelock.FileLock('/var/lock/LCK..{}'.format(SERIAL_DEVICE))
SMSD_PID_PATH = '/var/run/smstools/smsd.pid'
MODEM_RESET_TIMESTAMP_PATH = '../modem_last_reset'

RESET_MODEM = '/opt/httpapi/reset_modem.sh'

# initialization
app = Flask(__name__)

# extensions
auth = HTTPBasicAuth()

# Read config file
app.config.from_object('config')


def get_creg():
    try:
        with SLOCK.acquire(timeout=20):
            try:
                with serial.Serial(SERIAL_DEVICE_PATH, timeout=1) as device:
                    device.write(b'AT+CREG?\r\n')
                    creg = device.readline()
                    if creg.startswith(b'AT'):  # echo
                        creg = device.readline()
                    ok = device.readline().strip()
                    return creg, ok
            except serial.SerialException as e:
                app.logger.warning("unable to read from serial device: %s", e)
                return None, False
    except filelock.Timeout as e:
        app.logger.warning("couldn't obtain lock for serial device: %s", e)
        return None, False


def get_csq():
    try:
        with SLOCK.acquire(timeout=20):
            try:
                with serial.Serial(SERIAL_DEVICE_PATH, timeout=1) as device:
                    device.write(b'AT+CSQ\r\n')
                    csq = device.readline()
                    if csq.startswith(b'AT'):  # we got an echo
                        csq = device.readline()
                    ok = device.readline().strip()
                    return csq, ok
            except serial.SerialException as e:
                app.logger.warning("unable to read from serial device: %s", e)
                return 0, False
    except filelock.Timeout as e:
        app.logger.warning("couldn't obtain lock for serial device: %s", e)
        return 0, False


def parse_csq(csq):
    csq_match = CSQ_REGEX.match(csq)
    if csq_match is None:
        app.logger.info("csq ( {} ) didn't match regex ( {} )".format(csq, CSQ_REGEX.pattern))
        return 0
    else:
        return int(csq_match.group(1) or 0)


def parse_creg(creg):
    creg_match = CREG_REGEX.match(creg)
    if creg_match is None:
        app.logger.info("creg ( %s ) didn't match regex ( %s )", creg, CREG_REGEX.pattern)
        return -1
    else:
        return int(creg_match.group(1) or -1)


def get_modem_reset_file():
    created = False
    try:
        reset_file = open(MODEM_RESET_TIMESTAMP_PATH, 'r+')
    except IOError:
        reset_file = open(MODEM_RESET_TIMESTAMP_PATH, 'w')
        created = True

    return reset_file, created


def perform_reset():
    app.logger.info('resetting modem...')
    try:
        with SLOCK.acquire(timeout=20):
            subprocess.Popen((RESET_MODEM,))
    except filelock.Timeout:
        app.logger.warning('perform_reset: unable to obtain lock for serial device')


def reset_modem():
    now = time.time()
    reset_file, created = get_modem_reset_file()
    last = None

    with reset_file:
        if created:
            last = 0
        else:
            last_from_file = reset_file.read()
            try:
                last = float(last_from_file.strip())
            except ValueError:
                last = 0

        app.logger.info('last modem reset: %d', last)

        if now - last > app.config.get('MODEM_MINIMUM_RESET_INTERVAL', 300):
            perform_reset()
            reset_file.seek(0)
            reset_file.write(str(time.time()))
            reset_file.truncate()


if app.config.get('HASHED_PASSWORDS', False):
    iterations = app.config.get('DEFAULT_HASH_ITERATIONS', 10000)
    default_hash = 'pbkdf2:sha1:{}$_$'.format(iterations)  # invalid hash (if _ is never a salt)
    @auth.verify_password
    def verify(username, password):
        # if the user doesn't exist, check against the invalid hash anyway to avoid a timing side-channel
        hashed_password = app.config['USERS'].get(username, default_hash)
        return check_password_hash(hashed_password, password)


@app.before_request
def rewrite_auth_params():
    if ((not request.environ.get('HTTP_AUTHORIZATION', False)) and
                'username' in request.args and
                'password' in request.args):
        username, password = request.args['username'], request.args['password']
        request.environ['HTTP_AUTHORIZATION'] = 'basic ' + b64encode(username + ':' + password)


# Setup logging
if not app.debug:
    import logging

    level = logging.DEBUG
    logFormatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    app.logger.setLevel(level)
    app.logger.addHandler(consoleHandler)

def write_sms(sms):
    result = {}
    result['message_id'] = {}
    parts_count = 1
    ucs_field = False

    for mobile in sms['mobiles']:
        if not validate_mobile(mobile):
            app.logger.info('Mobile phone %s is not valid [%s]' % (mobile, auth.username()))
            result[mobile] = 'Not valid'
            continue
        if access_mobile(mobile):
            msg_file_lock=tempfile.mkstemp(dir=app.config['OUTGOING'],prefix=app.config['PREFIX'],suffix='.LOCK')[1]
            msg_file = msg_file_lock.split('.LOCK')[0]
            msg_len=len(sms['text'])
            try:
                msg = sms['text'].encode('us-ascii')
                if msg_len > 160:
                    parts_count = msg_len / 153 + (msg_len % 153 > 0)
            except UnicodeEncodeError:
                ucs_field = True
                msg = sms['text'].encode('utf-16-be')
                if msg_len > 70:
                    parts_count = msg_len / 67 + (msg_len % 67 > 0)
            with open(msg_file_lock, 'w') as f:
                f.write('From: ' + auth.username() + '\n')
                if ucs_field:
                    f.write('Alphabet: UCS\n')
                f.write('To: ' + mobile + '\n\n')
                f.write(msg)
                f.close()
                os.rename(msg_file_lock, msg_file)
                os.chmod(msg_file, 0666)
                app.logger.info('Message from %s to %s placed to the spooler %s' % (auth.username(), mobile, msg_file))
                message_id = msg_file.split('/')[-1]
            result['message_id'][mobile] = message_id
        else:
            app.logger.info('Forbidden to send message from %s to %s' % (auth.username(), mobile))
            result['message_id'][mobile] = 'Forbidden'
    result['sent_text'] = sms['text']
    result['parts_count'] = parts_count
    return result

def access_mobile(mobile):
    username = auth.username()
    if app.config.has_key('MOBILE_PERMS') and app.config['MOBILE_PERMS'].has_key(username): 
        if mobile in app.config['MOBILE_PERMS'].get(username):
            return True
        else:
            return None
    return True

def validate_mobile(mobile):
    if mobile.isdigit():
        return True
    return False

def bad_request(message):
    response = jsonify({'error': message})
    response.status_code = 400
    return response
    
@auth.get_password
def get_password(username):
    if username in app.config['USERS']:
        return app.config['USERS'].get(username)
    return None

@auth.error_handler
def unauthorized():
    return make_response(jsonify({'error': 'Unauthorized access'}), 401)

@app.route('/api/v1.0/sms', methods=['GET'])
def get_sent_sms():
    return 'OK'

@app.route('/api/v1.0/sms/sent/<path:message_id>', methods=['GET'])
@auth.login_required
def get_sms(message_id):
    sent_dir = app.config['SENT'] + '/'
    msg_fields = { 'From': None, 'To': None, 'Sent': None, 'message_id': message_id }

    try:
        with open(sent_dir + message_id) as f:
            for line in f:
                for field in msg_fields:
                    s_field = field + ': '
                    if line.startswith(s_field):
                        msg_fields[field] = line.split(s_field)[1].rstrip()
                    if line.startswith('\n'):
                        break
        return jsonify(msg_fields)
    except EnvironmentError:
        return not_found(404)

@app.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)

@app.errorhandler(500)
def internal_error(exception):
    return make_response(jsonify({'error': 'Internal error'}), 500)

@app.route('/api/v1.0/sms/outgoing', methods=['POST'])
@auth.login_required
def create_sms():
    if not request.json:
        return bad_request('Request error')

    if type(request.json) != dict:
        return bad_request('Request error')

    if not request.json.has_key('mobiles'):
        return bad_request('Request error')

    if not request.json.has_key('text') or type(request.json['text']) != unicode:
        return bad_request('Request error')

    for mobile in request.json['mobiles']:
        if type(mobile) != unicode:
            return bad_request('Request error')

    sms = {
        'mobiles': request.json['mobiles'],
        'text': request.json['text'],
    }

    result = write_sms(sms)
    return jsonify(result), 201


@app.route('/api/v1.0/sms/simple_send', methods=['GET'])
@auth.login_required
def simple_send_sms():
    if not request.args:
        return bad_request('missing required params')
    if not request.args.has_key('to'):
        return bad_request('missing required params')
    if not request.args.has_key('text'):
        return bad_request('missing required params')

    app.logger.debug('to: {}'.format(request.args.getlist('to')))

    sms = {
        'mobiles': request.args.getlist('to'),
        'text': request.args['text']
    }

    app.logger.info(sms)

    result = write_sms(sms)
    return jsonify(result), 201


@app.route('/api/v1.0/sms/modem_status', methods=['GET'])
@auth.login_required
def modem_status():
    csq, ok = get_csq()

    if not ok:
        return jsonify({'error': 'modem not available'}), 500

    csq_status = parse_csq(csq)

    if not 10 <= csq_status < 99:
        app.logger.warning("CSQ result: %s", csq)
        if csq_status == 99:
            reset_modem()
        return jsonify({'error': 'modem not connected or weak signal'}), 500

    creg, ok = get_creg()
    if not ok:
        return jsonify({'error': 'modem not available'}), 500

    creg_status = parse_creg(creg)
    if creg_status not in (1, 5):
        return jsonify({'error': 'registration problem'}), 500


@app.route('/api/v1.0/sms/smsd_status', methods=['GET'])
@auth.login_required
def smsd_status():
    with open(SMSD_PID_PATH, 'r') as pidfile:
        pid = int(pidfile.read().strip())
        try:
            os.kill(pid, 0)
        except OSError:
            return jsonify({'error': 'smsd not running!'}), 500
        else:
            return jsonify({'result': 'All OK'}), 200
