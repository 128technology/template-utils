#!/usr/bin/env python3

import argparse
import json
from os.path import basename, dirname, splitext
import re
import requests
import sys
import time
import urllib3
import yaml


DEBUG = False


class Conductor:
    """Helper class for REST API."""
    def __init__(self, host, username, password, force, revert, verify):
        self.host = host
        self.username = username
        self.password = password
        self.force = force
        self.revert = revert
        self.session = requests.Session()
        self.verify = verify

    def login(self):
        r = self.post('login',
                      {'username': self.username, 'password': self.password})
        if not r.ok:
            error('Could not log into conductor. Wrong password?')
        self.token = r.json()['token']
        self.session.headers["authorization"] = 'Bearer {}'.format(self.token)

    def get(self, location):
        location = location.strip('/')
        url = 'https://{}/api/v1/{}'.format(self.host, location)
        r = self.session.get(url, verify=self.verify)
        if not r.ok:
            warn(r.reason, r.text)
        return r

    def patch(self, location, json):
        location = location.strip('/')
        url = 'https://{}/api/v1/{}'.format(self.host, location)
        r = self.session.patch(url, json=json, verify=self.verify)
        if not r.ok:
            warn(r.reason, r.text)
        return r

    def post(self, location, json=None):
        location = location.strip('/')
        url = 'https://{}/api/v1/{}'.format(self.host, location)
        r = self.session.post(url, json=json, verify=self.verify)
        if not r.ok:
            warn(r.reason, r.text, '(at {})'.format(url))
        return r

    def restore_config(self, name):
        """Restore conductor configuration from backup."""
        r = self.post('config/import', {'filename': name})
        if not r.ok:
            error('Backup config could not be found:', name)

    def get_templates(self):
        """Retrieve templates from conductor."""
        templates = []
        r = self.get('template')
        if r.ok:
            templates = [t['name'] for t in r.json()]
        return templates

    def upload_template(self, template_file, data_file):
        """Upload a template from file to conductor."""
        base_name = basename(template_file)
        self.template_name = splitext(base_name)[0]
        content = load_json_yaml(template_file)
        # if json/yaml is invalid load raw file
        if content:
            instance_template = replace_template(json.dumps(content, indent=4))
            debug('template:', instance_template)
        else:
            with open(template_file) as fd:
                instance_template = fd.read()

        template = '\n'.join((
            '{% for instance in instances %}',
            '{% editgroup %}',
            instance_template,
            '{% endfor %}'
        ))

        variables = load_json_yaml(data_file)

        # check variables for consistency
        if 'instances' not in variables:
            raise InstancesMissing()

        data = {
            'name': self.template_name,
            'description': 'uploaded from {}'.format(base_name),
            'body': template.strip('\n'),
            'variables': variables,
        }

        print('Uploading template "{}"...'.format(self.template_name))
        # if template exists: quit unless force is set
        if self.template_name in self.get_templates():
            if not self.force:
                error('Template already exists on the conductor',
                      'Use --force to override.')
            self.patch('template/{}'.format(self.template_name), data)

        else:
            data['mode'] = 'ADVANCED'
            self.post('template', data)

    def render_template(self):
        """Render a template to 128T config."""
        if self.revert:
            r = self.post('/config/revertToRunning')
        print('Generating configuration...')
        progress(0)
        r = self.post('template/{}/generate'.format(self.template_name))
        gen_id = r.json()['id']
        finished = False
        while not finished:
            r = self.get('template/{}/generationStatus/{}'.format(
                self.template_name, gen_id))
            if r.json()['status'] != 'FINISHED':
                progress(r.json()['percentComplete'])
                time.sleep(1)
                continue
            finished = True
            progress(r.json()['percentComplete'])
            print('')

        if r.json()['errors']:
            error('There was an issue during config generation:', r.json())

    def validate(self):
        """Validate the 128T candidate config."""
        r = self.post('/config/validate')
        if r.json():
            error('There was an issue during validation:\n',
                  '\n '.join(['- {message}'.format(**d) for d in r.json()]))

    def commit(self):
        """Commit the 128T candidate config."""
        r = self.post('/config/commit')
        if r.json():
            error('There was an issue during commit:\n',
                  '\n '.join(['- {message}'.format(**d) for d in r.json()]))


def debug(*message):
    """Print a debug message."""
    if DEBUG:
        print('DEBUG:', *message)


def error(*message):
    """Print an error message and quit."""
    print('ERROR:', *message)
    sys.exit(1)


def warn(*message):
    """Print a warning."""
    print('WARNING:', *message)


def load_json_yaml(filename):
    """Load file in json or yaml format."""
    with open(filename) as fd:
        content = fd.read()

        # try to interpret content as json
        try:
            data = json.loads(content)
            return data
        except ValueError:
            pass

        # ... if json fails try yaml
        try:
            data = yaml.safe_load(content)
            return data
        except (ValueError, yaml.scanner.ScannerError):
            pass

        # ... None when both attempts fail
        return None


def replace_template(t):
    """Replace template."""
    t = re.sub(r'(\s+){\n(\s+)"placeholder": "assign (.*)"\n(\s+)},',
               r'\1{%- assign \3 %}', t)

    t = re.sub(r'{\n\s+"placeholder": "beginfor (.+) with (.+)"\n(\s+)},',
               r'{%- assign _first = false %}\n\3{%- for \1 %}\n\3{%- if \2 %}\n\3{%- if _first == false %}{% assign _first = true %}{% else %},{% endif %}', t)
    t = re.sub(r'},\n(\s+){\n\s+"placeholder": "endforwith"\n\s+}',
               r'}\n\1{%- endif %}\n\1{%- endfor %}', t)


    t = re.sub(r'{\n\s+"placeholder": "beginfor([^ ]*) (.+)"\n(\s+)},',
               r'{%- assign \1_first = false %}\n\3{%- for \2 %}\n\3{%- if \1_first == false %}{% assign \1_first = true %}{% else %},{% endif %}', t)

    t = re.sub(r'(}?),\n(\s+){\n\s+"placeholder": "endfor"\n\s+}',
               r'\1\n\2{%- endfor %}', t)

    t = re.sub(r'\[(,?)\n(\s+){\n(\s+)"placeholder": "beginif (.+)"\n(\s+)},',
               r'[\n\2{%- if \4 %}\1', t)

    t = re.sub(r'}(,?)\n(\s+){\n(\s+)"placeholder": "beginif (.+)"\n(\s+)},',
               r'}\n\2{%- if \4 %}\1', t)

    t = re.sub(r'}(,?)\n(\s+){\n(\s+)"placeholder": "else"\n(\s+)},',
               r'}\n\2{%- else %}', t)

    t = re.sub(r'},\n(\s+){\n(\s+)"placeholder": "endif"\n(\s+)}\n\s+{%- endfor %}',
               r'}\n\1{%- endif %}\n\1{%- endfor %}', t)

    t = re.sub(r'}(,?)\n(\s+){\n(\s+)"placeholder": "endif"\n(\s+)},?',
               r'}\1\n\2{%- endif %}', t)

    t = re.sub(r'},\n(\s+){\n(\s+)"placeholder": "endif_last"\n(\s+)},?',
               r'}\n\1{%- endif %}', t)


    t = re.sub(r'(\n\s+)"assign_.*": "(.+)",?\n',
               r'\1{%- assign \2 %}\n', t)

    # why is this needed? (question mark in regex above does not seem to work...)
    t = re.sub(r'(\n\s+)"assign_.*": "(.+)",\n',
               r'\1{%- assign \2 %}\n', t)

    t = re.sub(r'(,?)(\n\s+)"beginif_.*": "(.+)",\n',
               r'\1\2{%- if \3 %}\n', t)

    t = re.sub(r',(\n\s+)"elsif_last_.*": "(.+)",\n',
               r'\1{%- elsif \2 %}\n', t)
    t = re.sub(r'(,?)(\n\s+)"elsif_.*": "(.+)",\n',
               r'\1\2{%- elsif \3 %}\n', t)

    t = re.sub(r',?(\n\s+)"else_.*": [^,]+(,?)\n',
               r'\1{%- else %}\n', t)

    t = re.sub(r',(\n\s+)"endif_.*": [^,\n}]+(,?)\n',
               r'\2\1{%- endif %}\n', t)
    t = re.sub(r'(\n\s+)"endif_.*": [^,\n}]+,\n',
               r'\1{%- endif %}\n', t)

    return t


def parse_arguments():
    """Get commandline arguments."""
    parser = argparse.ArgumentParser(
        description='Upload and render 128T configuration templates')
    parser.add_argument('--conductor', '-c', required=True,
                        help='conductor host')
    parser.add_argument('--username', '-u', default='admin',
                        help='conductor username (default: admin)')
    parser.add_argument('--password', '-p', default='128Tadmin',
                        help='conductor password')
    parser.add_argument('--template', '-t', required=True,
                        help='template file')
    parser.add_argument('--data', '-d', help='data file', required=True)
    parser.add_argument('--validate', action='store_true',
                        help='validate configuration changes')
    parser.add_argument('--commit', action='store_true',
                        help='commit configuration changes')
    parser.add_argument('--revert', '-r', action='store_true',
                        help='revert config to running before apply template')
    parser.add_argument('--restore',
                        help='restore backup config prior to apply the template')
    parser.add_argument('--insecure', action='store_true',
                        help='skip TLS certificate validation')
    parser.add_argument('--force', '-f', action='store_true',
                        help='force template upload')
    parser.add_argument('--debug', action='store_true',
                        help='show debug output')
    return parser.parse_args()


def progress(count, total=100, status='', bar_len=60):
    filled_len = int(round(bar_len * count / float(total)))
    percents = round(100.0 * count / float(total), 0)
    fmt = '[{:-<60}] {:3.0f} %'.format('='*filled_len, percents)
    print('\b' * len(fmt), end='')  # clears the line
    sys.stdout.write(fmt)
    sys.stdout.flush()


def main():
    global DEBUG
    args = parse_arguments()
    DEBUG = args.debug
    verify=True
    if args.insecure:
        urllib3.disable_warnings()
        verify=False

    conductor = Conductor(
        args.conductor, args.username, args.password, args.force, args.revert, verify)
    conductor.login()

    if args.restore:
        conductor.restore_config(args.restore)
    conductor.upload_template(args.template, args.data)
    conductor.render_template()
    if args.validate:
        conductor.validate()
    if args.commit:
        conductor.commit()


if __name__ == '__main__':
    main()
