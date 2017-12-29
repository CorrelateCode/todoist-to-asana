import dateutil.parser
import json
import mimetypes
import os
from six import print_
import sys
import urlparse

import asana
import requests
import todoist

# below would be useful but barfs on unicode, despite never manipulating the args. arg!
# class Logger(object):
#     def __init__(self):
#         self.terminal = sys.stdout
#         self.log = open("etl.log", "a")
#     def write(self, message):
#         self.terminal.write(message)
#         self.log.write(message)
# sys.stdout = Logger()

ENV_PREFIX = "CORRELATE_"

def conf(var):
    return os.environ.get(ENV_PREFIX + var)

# below are set by sourcing variables.sh
TECH_TEAM = conf("ASANA_TECH_TEAM")
IMP_TEAM = conf("ASANA_IMP_TEAM")
WORKSPACE = conf("ASANA_WORKSPACE")
ASANA_PAT = conf("ASANA_RAY_PERSONAL_ACCESS_TOKEN")
TODOIST_USER = conf("TODOIST_USER")
TODOIST_PASS = conf("TODOIST_PASS")

USE_CACHED_TODOIST_DATA = True

mimetypes.init()

# ~ ~ ~ ~ Todoist wrappers

def get_todoist_data():
    api = todoist.TodoistAPI()
    user = api.user.login(TODOIST_USER, TODOIST_PASS)
    print("connected as: ", user['full_name'])
    return api.sync()


Todoist_categories = [
    "collaborators", "filters", "day_orders_timestamp",
    "live_notifications_last_read_id", "items", "temp_id_mapping", "labels",
    "reminders", "locations", "project_notes", "user", "full_sync",
    "live_notifications", "day_orders", "sync_token", "collaborator_states",
    "notes", "projects"
]


def get_category(category):
    if category not in Todoist_categories:
        raise ValueError("%s is not a valid todoist category" % (category))
    return [x for x in response[category]]


def save_todoist_json(response):
    todoist_data = json.dumps(response)
    f = open('todoist_data.json', 'w')
    f.write(todoist_data)
    f.close()


def cached_todoist_data():
    with open('todoist_data.json', 'r') as f:
        return json.load(f)


def todoist_user(id):
    author = [x for x in get_category('collaborators') if x['id'] == id]
    if len(author) > 0:
        return author[0]
    else:
        return {'full_name': 'unknown'}


def download_todoist_attachment(url):
    print("Download todoist attachment\n")
    try:  # site may have gone away
        r = requests.get(url)
    except Exception, e:
        return None, None
    return urlparse.urlsplit(url).path.split('/')[-1], r.content


# ~ ~ ~ ~ Asana wrappers


def get_asana_client(token):
    client = asana.Client.access_token(token)
    me = client.users.me()
    print_("me=" + json.dumps(me, indent=2))
    return client

    # find your "Personal Projects" workspace
    # try:
    #     personal_projects = next(workspace for workspace in me['workspaces']
    #                              if workspace['name'] == 'Personal Projects')
    #     projects = client.projects.find_by_workspace(personal_projects['id'], iterator_type=None)
    #     print_("personal projects=" + json.dumps(projects, indent=2))
    # except Exception, e:
    #     print_("No personal projects found")


def attach_file_to_asana_task(asana_task, todoist_note):
    print("Attach file to asana task\n")
    # fname = '/Users/ray/work/public_html/cambria-and-hike/p1010065.jpg'
    # with open(fname, 'r') as f:
    #     data = f.read()
    fa = todoist_note['file_attachment']
    file_name = fa.get('file_name', '')
    file_type = fa.get('file_type', None)
    file_url = fa.get('file_url') or fa.get('url')
    fname, data = download_todoist_attachment(file_url)
    if data is not None:
        if file_type is None:
            file_type, e = mimetypes.guess_type(file_url)
        try:
            attachment = client.attachments.create_on_task(
                asana_task['id'],
                file_name=file_name,
                file_content_type=file_type,
                file_content=data)
            return attachment
        except Exception, e:
            t, e = mimetypes.guess_type(file_url)
            print("Couldn't attach %s to task" % (todoist_note))
            print("fname %s, mimetype %s\n" % (fname, t))
            # print("data: %s\n" %(data))
    return None


def append_note_to_asana_task(asana_task, note):
    print("Append note: %s\n" % (note))
    f = note['file_attachment']

    author_id = todoist_user(note['posted_uid'])
    author = "[ todoist-imp: Original author %s ]\n" % (
        author_id.get('full_name', 'unknown'))

    text = note['content'] + "\n"

    if f is not None and f['resource_type'] in ('website'):
        text += f.get('description', '') + "\n"
        text += f.get('site_name', '') + "\n"
        text += f.get('title', '') + "\n"
        text += f.get('url', '') + "\n"

    if f is not None and f.get('file_type', '') == 'application/octet-stream':
        text += f['file_name'] + "\n"
        if f['file_url'] != note['content']:
            text += f['file_url'] + "\n"
        f = None

    # first, move over any note text
    if text != '':
        text = author + text
        client.stories.create_on_task(task=asana_task['id'], text=text)

    # second, move the attachment if it exists
    if f is not None:
        if f['resource_type'] in ('file', 'image'):
            attach_file_to_asana_task(asana_task, note)


def create_asana_task(asana_proj, todoist_task):
    t = todoist_task
    name = t['content']
    due_on = None
    print("Create task: %s\n" % (todoist_task))

    if t['due_date_utc'] is not None:
        due_on = dateutil.parser.parse(t['due_date_utc']).isoformat()

    result = client.tasks.create_in_workspace(
        WORKSPACE,
        {
            'name': name,
            # 'notes': 'Note: This is a test task created with the python-asana client.',
            'projects': [asana_proj['id']],
            'due_on': due_on,
            'completed': t['checked'] and True or False
        })

    return result


def create_asana_project(name, team):
    name = 'Todoist-imp: ' + name
    print("Create project: %s\n" % (name))
    proj = client.projects.create_in_team(team, {'name': name})
    return proj


def recreate_todoist_project_in_asana(client, proj):
    print("Recreate todoist project: %s\n" % (proj['name']))
    todoist_items = get_category('items')
    todoist_notes = get_category('notes')

    asana_proj = create_asana_project(proj['name'], IMP_TEAM)

    items = [x for x in todoist_items if x['project_id'] == proj['id']]
    for item in items:
        asana_task = create_asana_task(asana_proj, item)
        item_notes = [x for x in todoist_notes if x['item_id'] == item['id']]
        for note in item_notes:
            append_note_to_asana_task(asana_task, note)


def recreate_todoist_projects_in_asana(client):
    print("Recreate todoist projects in asana\n")
    todoist_projects = get_category('projects')
    for proj in todoist_projects:
        x = 'y' # raw_input("Recreate project %s in asana? [y/n] " % (proj['name']))
        if x=='y':
            recreate_todoist_project_in_asana(client, proj)


if __name__ == "__main__":
    print("Logging into asana")
    client = get_asana_client(CORRELATE_ASANA_RAY_PERSONAL_ACCESS_TOKEN)

    print("Retrieving todoist data")
    if USE_CACHED_TODOIST_DATA:
        response = cached_todoist_data()
    else:
        response = get_todoist_data()
        print("Caching todoist data locally")
        save_todoist_json(response)

    recreate_todoist_projects_in_asana(client)
