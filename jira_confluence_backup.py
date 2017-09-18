import argparse
import datetime
import getpass
import json
import logging
import os
import time
from logging.handlers import SysLogHandler
from sys import stdout, exit

import requests


# Argparse action extension to alternatively accept passwords directly
# or via a prompt in the console


class Password(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
        if values is None:
            values = getpass.getpass()
        setattr(namespace, self.dest, values)


def set_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance',
                        '-i',
                        help='Atlassian account instance (format: \
                        account.attlassian.net)', required=True)
    parser.add_argument('--application', '-a',
                        help='Atlassian application to backup \
                        (Jira or Confluence)', required=True)
    parser.add_argument('--username', '-u',
                        help='Atlassian username used to \
                        login to the web application', required=True)
    parser.add_argument('--timeout', '-t',
                        help='Timeout for the remote backup to complete,\
                        defaults to 180 minutes',
                        default=180)
    parser.add_argument('--taskid', '-d',
                        help='Task id to operate upon',
                        default=None)
    parser.add_argument('--password', '-p',
                        action=Password,
                        nargs='?', dest='password',
                        help='Atlassian password used to login into the web \
                        application',
                        required=True)
    parser.add_argument('--location',
                        '-l',
                        default='/tmp/',
                        help='Location of resulting backup file, defaults to\
                        \'/tmp/APPLICATION\'')
    parser.add_argument('--tasks',
                        nargs='+',
                        help='Perform specified tasks (with a space between \
                        each task): t (trigger), m (monitor) and d (download).\
                        If d add argument for file name')
    parser.add_argument('--log',
                        help='Enable logging',
                        dest='log',
                        action='store_true')
    args = parser.parse_args()

    # Ensure valid tasks where supplied
    if args.tasks is not None:
        targs = ['t', 'm', 'd']
        if all((x not in args.tasks for x in targs)):
            print "Error: please supply either 't' (trigger), 'm' (monitor) or\
            d (download) with tasks"
            exit(1)
    return args


class LogMessage:
    '''
    Simple local syslog logger
    '''

    def __init__(self, application):
        self._logger = logging.getLogger('BackupLogger')
        self._logger.setLevel(logging.INFO)
        self._handler = logging.handlers.SysLogHandler(address='/dev/log')
        self._logger.addHandler(self._handler)
        self._tag = str.upper('ATLASSIAN_BACKUP_' + application)
        self._format = logging.Formatter('%(name)s %(tag)s: %(message)s')
        self._handler.setFormatter(self._format)
        self._logger.addHandler

    def log_message(self, message):
        global log
        if log:
            self._logger.info(message, extra={'tag': self._tag})


def set_urls():
    global trigger_url
    global progress_url
    global download_url
    if application.upper() == 'CONFLUENCE':
        trigger_url = 'https://' + instance + '/wiki/rest/obm/1.0/runbackup'
        progress_url = 'https://' + instance + \
                       '/wiki/rest/obm/1.0/getprogress.json'
        download_url = 'https://' + instance + '/wiki/download/'
        return
    if application.upper() == 'JIRA':
        trigger_url = 'https://' + instance + '/rest/backup/1/export/runbackup'
        progress_url = 'https://' + instance + '/rest/internal/2/task/progress/{0}'
        download_url = 'https://' + instance + '/plugins/servlet/export/download/'
        return
    if application.upper() != 'JIRA' or 'CONFLUENCE':
        print "Invalid application specified. \
        Request either \"Jira\" or \"Confluence\""
        exit(1)


def create_session(username, password):
    s = requests.session()
    # create a session
    url = 'https://' + instance + '/rest/auth/1/session'
    print url
    r = s.post(url=url,
               data=json.dumps({'username': username, 'password': password}),
               headers={'Content-Type': 'application/json'})
    if int(r.status_code) == 200:
        return s
    else:
        print "Session creation failed"
        print int(r.status_code)
        print r.content
        exit(1)


def get_last_task_id(s):
    url = 'https://{0}/rest/backup/1/export/lastTaskId'.format(instance)
    r = s.get(url=url)
    if int(r.status_code) == 200:
        return str(r.text)

    return None


def trigger(s):
    global taskId
    postData = json.dumps({'cbAttachments': 'true', 'exportToCloud': 'true'})
    headers = {'Content-Type': 'application/json',
               'accept': 'application/json, text/javascript, */*; q=0.01',
               'X-Atlassian-Token': 'no-check',
               'pragma': 'no-cache',
               'cache-control': 'no-cache',
               'authority': instance,
               'X-Requested-With': 'XMLHttpRequest'}

    r = s.post(url=trigger_url,
               data=postData,
               headers=headers)
    print "Trigger response: %s" % r.status_code
    if int(r.status_code) == 200:
        print "Trigger response successful"
        if application.upper() == 'JIRA':
            json_data = json.loads(r.text)
            try:
                taskId = json_data['taskId']
            except:
                bkp_err = json_data['error']
                result = ['Trigger failed with message: %s' % str(bkp_err), False]
        result = ['Trigger response successful for task %s' % str(taskId), True]
        return result
    else:
        print 'Trigger failed'
        if int(r.status_code) == 500:
            print('Returned text data: %s' % str(r.text))
            result = ['Trigger failed with message: %s' % str(r.text), False]
        elif int(r.status_code) == 412:
            json_data = json.loads(r.text)
            print('Returned text data: %s' % str(json_data['error']))
            result = ['Trigger failed with message: %s' % str(json_data['error']), False]
        return result


def monitor(s):
    global progress_url, taskId
    if application.upper() == 'JIRA':
        if taskId is None:
            print("Trying to retrieve task ID from JIRA..")
            taskId = get_last_task_id(s)
            if taskId is None:
                return ['Monitor failed due to missing taskId', False]

        # Append the task id to the job
        progress_url = progress_url.format(taskId)

    r = s.get(url=progress_url)
    try:
        progress_data = json.loads(r.text)
    except ValueError:
        print """No JSON object could be decoded.
        Get progress failed to return expected data.
        Return code: %s """ % (r.status_code)
        result = ['No JSON object could be decoded\
            - get progress failed to return expected data\
        Return code: %s """ % (r.status_code)', False]
    # Timeout waiting for remote backup to complete
    # (since it sometimes fails) in 5s multiples

    global timeout
    timeout_count = timeout * 12  # timeout x 12 = number of iterations of 5s
    time_left = timeout

    completed = False

    nested_data = None
    if 'result' in progress_data:
        nested_data = json.loads(progress_data['result'])

    while not completed:
        # Clears the line before re-writing to avoid artifacts
        stdout.write("\r\x1b[2k")
        stdout.write("\r\x1b[2K%s (%s). Timeout remaining: %sm"
                     % (str(
            progress_data['progress'] if 'progress' in progress_data else str(progress_data['alternativePercentage'])),
                        str(progress_data['description']) if 'description' in progress_data else str(
                            progress_data['currentStatus']),
                        str(time_left)))
        stdout.flush()
        r = s.get(url=progress_url)
        progress_data = json.loads(r.text)
        time.sleep(5)
        timeout_count = timeout_count - 5
        if timeout_count % 12 == 0:
            time_left = time_left - 1

        if nested_data and 'fileName' in nested_data:  # JIRA new infra
            completed = True
        elif 'fileName' in progress_data:  # Confluence
            completed = True
        elif timeout_count == 0:  # Falltrough
            completed = True

    # JIRA new infra
    if nested_data and 'fileName' in nested_data:
        result = [nested_data['fileName'], True]
        return result

    # Confluence (old) infra
    if 'fileName' in progress_data:
        result = [progress_data['fileName'], True]
        return result


def get_filename(s):
    global progress_url, taskId
    if application.upper() == 'JIRA':
        if taskId is None:
            print("Trying to retrieve task ID from JIRA..")
            taskId = get_last_task_id(s)
            if taskId is None:
                print 'Unable to obtain without taskid (JIRA).'
                return False

        # Append the task id to the job
        progress_url = progress_url.format(taskId)

    print "Fetching file name"

    r = s.get(url=progress_url)
    try:
        progress_data = json.loads(r.text)
    except ValueError:
        print """No JSON object could be decoded.
        Get progress failed to return expected data.
        Return code: %s """ % (r.status_code)
        return False

    if 'fileName' in progress_data:
        return progress_data['fileName']

    if 'result' in progress_data:
        nested_response = json.loads(progress_data['result'])
        if 'fileName' in nested_response:
            return nested_response['mediaFileId'] + '/' + nested_response['fileName']

    print 'File name to download not found in server response.'
    return False


def create_backup_location(l):
    if not os.path.exists(location):
        try:
            os.makedirs(location, 0744)
        except OSError:
            return False
    return True


def download(s, l):
    filename = get_filename(s)
    if not filename:
        return False
    print "Filename found: %s" % filename
    print "Checking if url is valid"

    r = s.get(url=download_url + filename, stream=True)
    print "Status code: %s" % str(r.status_code)

    if int(r.status_code) == 200:
        print "Url returned '200', downloading file"
        if not create_backup_location(l):
            result = ['Failed to create backup location', False]
            return result
        date_time = datetime.datetime.now().strftime("%Y%m%d")
        with open(l + '/' + application + '-' + date_time + '.zip', 'wb') as f:
            file_total = 0
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
                    file_total = file_total + 1024
                    file_total_m = float(file_total) / 1048576
                    # Clears the line before re-writing to avoid artifacts
                    stdout.write("\r\x1b[2k")
                    stdout.write("\r\x1b[2K%.2fMB   downloaded" % file_total_m)
                    stdout.flush()
        stdout.write("\n")
        result = ['Backup downloaded successfully', True]
        return result
    else:
        print "Download file not found on remote server - response code %s" % \
              str(r.status_code)
        print "Download url: %s" % download_url + filename
        result = ['Download file not found on remote server', False]
        return result


if __name__ == "__main__":
    args = set_arguments()
    global trigger_url, progress_url, download_url, \
        instance, application, log, timeout
    application = args.application
    instance = args.instance + ".atlassian.net"
    username = args.username
    password = args.password
    location = args.location
    timeout = int(args.timeout)
    taskId = args.taskid

    if args.log:
        log = True

    set_urls()

    log = LogMessage(application)

    session = create_session(username, password)

    if args.tasks is None or 't' in args.tasks:
        print "Triggering backup"
        result = trigger(session)
        log.log_message(result[0])
        if not result[1]:
            exit(1)

    if args.tasks is None or 'm' in args.tasks:
        print "Monitoring remote backup progress"
        result = monitor(session)
        log.log_message(result[0])
        if not result[1]:
            exit(1)

    if args.tasks is None or 'd' in args.tasks:
        print "Downloading file"
        result = download(session, location)
        log.log_message(result[0])
        if not result[1]:
            exit(1)

    log.log_message('All tasks completed successfully')
    print "All tasks completed."
