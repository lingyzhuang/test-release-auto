#!/usr/bin/python
"""
This is a script for monitoring the PP page's new release milestones and triggering
related release configuration actions.
"""

import subprocess
import argparse
import datetime
import time
import os
import yaml
import re
import json

from utils.requests_session import requests_session
from constants import JIRA_URLs, PP_REST, ET_URL, RELEASE_METADATA_DIR, \
                      PCT_MR_URL, SSL_CERT, JIRA_TOKEN, PCT_API_TOKEN, \
                      JIRA_PROJECTS, SERVICE_ACCOUNT, RHELCMP_PRE_JOB_TOKEN, \
                      RHELCMP_JENKINS_TOKEN
from jira_connection import JiraConnection


TRIGGER_DATE = datetime.datetime.now().date().strftime('%Y-%m-%d')
DRY_RUN = False
JIRA_CONNECTION = None
AUTO_MERGE = False

session = requests_session()
no_auth_session = requests_session(False)


def convert_et_release_names(pp_short_name):
    release_meta = pp_short_name.split("-")

    # Zstream configuration
    if ".z" in pp_short_name:
        # The RHEL-10 and newer only have RHEL-10.0.Z
        if len(release_meta) == 2:
            return f"{release_meta[0]}-{release_meta[1]}"

        # RHLE-9 and older versions
        if int(release_meta[2].split(".")[0]) % 2 == 0:
            return f"{release_meta[0]}-{release_meta[1]}.{release_meta[2]}.MAIN+EUS"
        return f"{release_meta[0]}-{release_meta[1]}.{release_meta[2]}.MAIN"

    # The logic start form 10, so only beta supports from 10, so no X-Y.Z.z style
    if "beta" in pp_short_name:
        return f"{release_meta[0]}-{release_meta[1].replace('public_beta', 'BETA')}"

    # The RHEL-8 RHEL-9 it's like rhel-9-4.0.z
    if len(release_meta) > 2:
        return f"{release_meta[0]}-{release_meta[1]}.{release_meta[2]}.GA"

    # In RHEL-10 and newer version it's RHEL-10.0
    return f"{release_meta[0]}-{release_meta[1]}"


def fetch_online_releases(product, phase, team="wf"):
    # https://pp.engineering.redhat.com/api/latest/releases/?product__shortname=rhel&phase_display=Concept
    res = no_auth_session.get(f"{PP_REST}releases/",
                              params={"product__shortname": product,
                                      "phase_display": phase["name"]})
    res.raise_for_status()
    releases = res.json()
    for r in releases:
        pp_short_name = r['shortname']
        short_name = convert_et_release_names(pp_short_name)
        print(f"[{short_name}] Check milestone and trigger tasks...")
        for milestone in phase["milestones"]:
            func = eval(milestone['action'])
            milestone["jira_project"] = JIRA_PROJECTS[team]
            func(r, milestone)


def match_pp_tasks(release_short_name, match_content, flag="sp"):
    # https://pp.engineering.redhat.com/api/latest/releases/rhel-9-5.0/schedule-tasks/?flags_in=sp
    res = no_auth_session.get(f"{PP_REST}"
                              f"releases/{release_short_name}/schedule-tasks/",
                              params={"flags_in": flag})
    tasks = res.json()
    matched_tasks = []
    for t in tasks:
        # Map the task either only we content or content - x.y
        # e.g.  New Release Configuration - 9.3
        if f"{match_content}" in t["name"] and t["release_shortname"] == release_short_name:
            matched_tasks.append(t)
        if ("rhel" in release_short_name) and ("compose" in t["name"] or "Compose" in t["name"]):
            matched_tasks.append(t)
    return matched_tasks


# Execute the PCT MR creation action
def trigger_pct_configuration(params, pct_ticket, milestone, auto_merge):
    if params["action"] == "phase1":
        try:
            subprocess.check_call(["bash",
                                   "rhelwf_scripts/utils/pct_mr_tool.sh",
                                   params["action"],
                                   params["commit_branch"],
                                   params["release"],
                                   params["ship_date"]])
        except Exception as e:
            raise_error(f'Executing error on pct_mr_tool.sh: {e}', pct_ticket.key)

    # Define custom headers and data
    headers = {
        'PRIVATE-TOKEN': PCT_API_TOKEN,
    }
    # Create a MR to the target_branch
    data = {
        'source_branch': params["commit_branch"],
        'target_branch': 'master',
        'title': params["commit_message"],
        'description': params["commit_message"],
    }

    # Make a POST request with custom headers and data
    mr_response = no_auth_session.post(f'{PCT_MR_URL}/merge_requests',
                                       headers=headers,
                                       json=data)

    # Monitor the MR with 2 hours timeout and try every 2 mins
    # The iid of the return data is the MR id.
    # There is a "merge_status":"can_be_merged" is about the patch can be merged.
    # The "detailed_merge_status":"ci_still_running" is also a checking condition
    mr_response_json = mr_response.json()
    iid = mr_response_json["iid"]

    max_retries = 60
    retry_interval = 120
    retry_count = 0
    commit_sha = ""

    print('Checking pipeline status with 2 hours timeout ...')
    while retry_count < max_retries:
        try:
            # e.g. https://gitlab.cee.redhat.com/api/v4/projects/37797/merge_requests/291
            mr_status = no_auth_session.get(f"{PCT_MR_URL}/merge_requests/{iid}",
                                            headers={"Content-type": "application/json",
                                                     "PRIVATE-TOKEN": PCT_API_TOKEN})
            mr_status_json = mr_status.json()
            # Check if the request was successful (HTTP status code 2xx)
            if mr_status.status_code // 100 == 2 and \
               mr_status_json["merge_status"] == "can_be_merged" and \
               mr_status_json["detailed_merge_status"] == "mergeable":
                # Merge the MR
                # Can enable full auto merge after a few rounds/different of releases got mr created
                # without any problem
                if auto_merge:
                    merge_status = no_auth_session.put(f"{PCT_MR_URL}/merge_requests/{iid}/merge",
                                                       headers={"PRIVATE-TOKEN": PCT_API_TOKEN})
                    if merge_status.status_code // 100 == 2:
                        merge_status_json = merge_status.json()
                        if merge_status_json["merge_commit_sha"]:
                            commit_sha = merge_status_json["merge_commit_sha"]
                        else:
                            commit_sha = merge_status_json["sha"]
                    else:
                        raise_error(f'Merge operation has some problem {mr_status.json()}',
                                    pct_ticket)
                # Exit the loop if the request was successful
                break

            # Increment the retry count
            retry_count += 1

        except Exception as e:
            raise_error(f'Merge Error: {e}', pct_ticket)

        # Wait for the specified interval before retrying
        time.sleep(retry_interval)

    else:
        raise_error('The pipeline still not passes after 2 hours, please check \
                    and handle the merge action manually',
                    pct_ticket)
    if auto_merge:
        return check_merge_pipeline_status(commit_sha, pct_ticket)
    else:
        # Human merge required
        return True


def check_merge_pipeline_status(commit_sha, pct_ticket):
    max_retries = 60
    retry_interval = 120
    retry_count = 0
    print('Checking merged pipeline status with 2 hours timeout ...')
    while retry_count < max_retries:
        try:
            merged_status = no_auth_session.get(f"{PCT_MR_URL}/pipelines",
                                                params={"sha": commit_sha},
                                                headers={"PRIVATE-TOKEN": PCT_API_TOKEN})
            merged_status_json = merged_status.json()
            # Check if the request was successful (HTTP status code 2xx)
            if merged_status.status_code // 100 == 2 and \
               all(p["status"] == "success" for p in merged_status_json):
                # Close the ticket when all the pipeline running success
                close_ticket(pct_ticket)
                break  # Exit the loop if the request was successful

            # Increment the retry count
            retry_count += 1

        except Exception as e:
            raise_error(f'Merge Error: {e}', pct_ticket)

        # Wait for the specified interval before retrying
        time.sleep(retry_interval)

    else:
        raise_error('Merged pipeline still not passes after 2 hours, \
                    please check manually', pct_ticket)

    return True


def raise_error(error_message, pct_ticket):
    JIRA_CONNECTION.comment(pct_ticket, error_message)
    raise BaseException(error_message)


# Create the ticket to track the releng task execution
def close_ticket(release_issue):
    JIRA_CONNECTION.comment(release_issue, "The merge pipeline finished successfully")
    for status in ["Review", "Resolved"]:
        JIRA_CONNECTION.change_status(release_issue, status)
    print(f'{release_issue.key} is closed')


def update_jira_issue_in_progress(release_issue):
    re_fetched_issue = JIRA_CONNECTION.issue(release_issue)
    issue_status = re_fetched_issue.raw["fields"]["status"]["name"]
    # change it to Groom if it's in ungroom status
    if issue_status == "New":
        JIRA_CONNECTION.change_status(release_issue, "Groom")

    # change status to in progress if it hasn't been changed
    if issue_status == "Refinement":
        JIRA_CONNECTION.change_status(release_issue, "Start Progress")

    print(f'Move {release_issue.key} to In Progress')


def check_and_create_task_tracker(r, task, milestone, summary=""):
    if milestone["jira_project"] != "RHELCMP":
        label = get_label(r, milestone)
        issue = search_issue(f"project={milestone['jira_project']} and labels={label}")
        if issue:
            print(f"{r['shortname']} - {issue.key} is created for the action {milestone['action']}")
            return issue
    else:
        label = "releng"
        issue = search_issue(f"project={milestone['jira_project']} and summary ~ \"{summary.replace('[', '\\\\[').replace(']', '\\\\]')}\" and issuetype != Sub-task and status != Closed AND status != Resolved")
        if issue:
            print(f"{r['shortname']} - {issue.key} is created.")
            return issue

    due_date = datetime.datetime.strptime(task['date_start'], "%Y-%m-%d")
    pipeline_triggering_date = datetime.datetime.strptime(TRIGGER_DATE, "%Y-%m-%d")

    # Check if date1 is 30 or more days after date2
    if (due_date - pipeline_triggering_date).days <= 30:
        if "issue_summary" in milestone:
            issue_summary = milestone["issue_summary"]
        else:
            if summary:
                issue_summary = summary
            else:
                issue_summary = f"{r['shortname']} - {task['name']}"
        if "issue_description" in milestone:
            issue_description = milestone["issue_description"]
        else:
            issue_description = f"{r['shortname']} - {task['name']}"
        if "issue_type" in milestone:
            issue_type = milestone["issue_type"]
        else:
            issue_type = "Task"

        issue = JIRA_CONNECTION.connection.create_issue(project=milestone["jira_project"],
                                                        summary=issue_summary,
                                                        description=issue_description,
                                                        issuetype=issue_type,
                                                        labels=[label],
                                                        duedate=task['date_start'])
        if "watchers" in milestone:
            for watcher in milestone["watchers"]:
                JIRA_CONNECTION.connection.add_watcher(issue, watcher)

        print(f"{r['shortname']} - {issue.key} is created for tracking the action")
        return issue
    else:
        print(f"Debug issue creating skip as target date is {task['date_start']}")


def search_issue(jira_filter):
    issues = JIRA_CONNECTION.connection.search_issues(jira_filter)
    if issues:
        return issues[0]
    else:
        return None


def get_label(r, milestone):
    if "jira_label_prefix" in milestone:
        return f"{milestone['jira_label_prefix']}-{r['shortname']}"
    else:
        return f"{milestone['action']}-{r['shortname']}"


def load_release_metadata(team):
    with open(f"{RELEASE_METADATA_DIR}/release_info_{team}.yml", 'r') as file:
        products = yaml.safe_load(file)

    return products


def is_dependency_ready(jira_issue, milestone):
    # The issue is on In Progress status, the pipeline will skip
    # The issue is on New and the labels are matched, return true to trigger pipeline
    re_fetched_issue = JIRA_CONNECTION.issue(jira_issue)

    if "dependency_labels" in milestone:
        return set(milestone["dependency_labels"]) <= \
                   set(re_fetched_issue.raw["fields"]["labels"])
    else:
        return True


def check_pct_mr_label(r, milestone, label_for_search):
    if "pct_mr_create_label" in milestone:
        issue = search_issue(f"project={milestone['jira_project']} and labels={label_for_search}")
        if issue:
            print(f"{r['shortname']} - PCT mr is already created for "
                  f"{issue.key}'s action {milestone['action']}")
            return issue
    return None


def check_and_create_pct_mr(r, milestone, params, tracking_issue):
    label_for_search = f"{milestone['pct_mr_create_label']}-{r['shortname']}"
    if check_pct_mr_label(r, milestone, label_for_search) is not None:
        print("WARN: Release is not in ET and mr created label exist, "
              "mr need to be reviewed and merged ASAP")
    else:
        try:
            trigger_pct_configuration(params, tracking_issue, milestone, AUTO_MERGE)
            if tracking_issue is not None and "pct_mr_create_label" in milestone:
                tracking_issue.update(labels=[{"add": label_for_search}])
        except Exception as e:
            raise_error(f'Trigger pct configuration Error: {e}', tracking_issue)


def print_and_comment(jira_issue, info_message):
    print("INFO: " + info_message)
    JIRA_CONNECTION.comment(jira_issue, info_message)


def load_execute_tasks(team):
    products = load_release_metadata(team)

    # Handle product's releases which are not skipted in yaml file
    if products:
        for product in products["products"]:
            for phase in product["phases"]:
                if not phase["skip"]:
                    print(f"Checking {product['name']}'s {phase['name']} releases and milestones")
                    fetch_online_releases(product["name"], phase, team)


# RHELWF actions start
# release configuration phase1(new release configuration milestone on PP)
def create_release_phase1(r, milestone):
    release_short_name = r["shortname"]
    short_name = convert_et_release_names(release_short_name)
    result = session.get(
            f"{ET_URL}api/v1/releases/",
            params={"filter[name]": short_name}
            )
    data = result.json()
    if data['data'] != []:
        # No action if there is already release created on the ET
        return

    tasks = match_pp_tasks(release_short_name, milestone['milestone'])
    if len(tasks) > 0:
        task = tasks[0]
        tracking_issue = check_and_create_task_tracker(r, task, milestone)
        if datetime.datetime.strptime(task['date_start'], '%Y-%m-%d') <= \
           datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d') \
           and is_dependency_ready(tracking_issue, milestone):
            if tracking_issue:
                issue_status = tracking_issue.raw["fields"]["status"]["name"]
                if issue_status in ["New", "Refinement"]:
                    update_jira_issue_in_progress(tracking_issue)
                else:
                    return
            version_list = release_short_name.split("-")
            # get the x.y from the shortname, different names on rhel 10 and 9
            # rhel-10.0, rhel-9-5.0
            x_y = version_list[1]
            if len(version_list) > 2:
                x_y = f"{version_list[1]}.{version_list[2].split('.')[0]}"
            if DRY_RUN:
                print(f"DEBUG: Calling ./scripts/create_variable_file.py --version RHEL-{x_y}"
                      f" --ship-date {r['ga_date']}")
            else:
                if tracking_issue:
                    info_message = (f"Create the create_release_phase1 patch into PCT for"
                                    f" {release_short_name} on {TRIGGER_DATE}")
                    print_and_comment(tracking_issue, info_message)
                    params = {"commit_branch": f"release-auotmation-RHEL-{x_y}-phase1",
                              "action": "phase1",
                              "release": f"RHEL-{x_y}",
                              "ship_date": r['ga_date'],
                              "commit_message": f"New Configuration RHEL-{x_y} with ship \
                                date {r['ga_date']}"}
                    check_and_create_pct_mr(r, milestone, params, tracking_issue)
                else:
                    print(f"DEBUG: issue creating skip as target date is over 30 days, "
                          f"it is {task['date_start']}")
        else:
            print(f"INFO: The {release_short_name} isn't on ET, the automation only create"
                  f" it on or after {task['date_start']} once dependency is solved.")
    else:
        print(f"INFO: No release configuration phase 1 action for the {release_short_name}")


# new release config phase 2 scheduled after phase1 (enable osci gating on PP)
def create_release_phase2(r, milestone):
    release_short_name = r["shortname"]
    tasks = match_pp_tasks(release_short_name, milestone['milestone'], flag=milestone["flag"])
    if len(tasks) > 0:
        task = tasks[0]
        tracking_issue = check_and_create_task_tracker(r, task, milestone)
        if datetime.datetime.strptime(task['date_start'], '%Y-%m-%d') <= \
           datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d') \
           and is_dependency_ready(tracking_issue, milestone):
            info_message = (f"Create the create_release_phase2 patch into PCT"
                            f" for {release_short_name} on {TRIGGER_DATE}")
            print_and_comment(tracking_issue, info_message)
            # TODO
            # Real logic for the action triggering
        else:
            print(f"INFO: Need to wait after {task['date_start']} and dependency solved"
                  f" for create_release_phase2 ...")
    else:
        print(f"INFO: No release configuration phase 2 action for the {release_short_name}")


# post GA tasks (After the release GA date, we don't have message about this yet)
def post_ga(r, create_release_phase1):
    print("INFO: TBD for POST GA")


# EUS set up (we typically schedule this after GA. e.g: after 9.3 GA, we'll
# start configuration for 9.4 EUS)
def create_extentions(r, create_release_phase1):
    print("INFO: TBD for create extention")


# POST RC tasks (Nightly compose promoted as RC compose message on the PP)
def post_rc(r, milestone):
    release_short_name = r["shortname"]
    tasks = match_pp_tasks(release_short_name, milestone['milestone'])
    if len(tasks) > 0:
        task = tasks[0]
        tracking_issue = check_and_create_task_tracker(r, task, milestone)
        if datetime.datetime.strptime(task['date_start'], '%Y-%m-%d') <= \
           datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d') \
           and is_dependency_ready(tracking_issue, milestone):
            info_message = f"Trigger POST RC action for {release_short_name} on {TRIGGER_DATE}"
            print_and_comment(tracking_issue, info_message)
            # TODO
            # Real logic for the action triggering
        else:
            print(f"INFO: Wait after {task['date_start']} and dependency solved to trigger "
                  f"the POST RC actions for {release_short_name} ...")
    else:
        print(f"INFO: No Post RC action for the {release_short_name}")


# EOL configuration (scheduled start date for these tasks happen day after
# official EOL date of a lifecycle)
def eol(r, create_release_phase1):
    print("INFO: TBD for EOL")


# Rules TBD
def create_async(r, create_release_phase1):
    print("INFO: TBD for create async")

# Get batch ID from release ID
def rhelcmp_get_batch_id(release_id, batch_id, issue):
    curl_command = f"curl -g -X GET -u \":\" --negotiate 'https://errata.devel.redhat.com/api/v1/batches?filter[release_id]={release_id}&filter[name]=*{batch_id}'"
    try:
        batch_info = subprocess.check_output(curl_command, shell=True)
        batch_id = json.loads(batch_info.decode('utf-8'))["data"][0]["id"]
        return batch_id
    except Exception as e:
         raise_error(f'Executing error on pct_mr_tool.sh: {e}', issue)

# RHELCMP team tasks
def rhelcmp_build_compose(r, milestone):
    release_short_name = r["shortname"]
    curl_main = f"curl -k -X POST 'https://{SERVICE_ACCOUNT}:{RHELCMP_JENKINS_TOKEN}@{milestone['jenkins_url']}/job"
    curl_token = f"buildWithParameters?"
    if release_short_name in milestone['compose_build_products']:
        tasks = match_pp_tasks(release_short_name, milestone['milestone'])
        if len(tasks) > 0:
            for task in tasks:
                # Only handle not started composes
                if datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d') <= \
                    datetime.datetime.strptime(task['date_start'], '%Y-%m-%d'):
                    if ('.z' in release_short_name):
                        release_short_name = f"{release_short_name[:-2]}"

                    version_list = release_short_name.split('-')
                    if (len(version_list) == 3):
                        x_y_z = f"{version_list[1]}.{version_list[2]}"
                    elif (len(version_list) == 2):
                        x_y_z = version_list[1]
                    x_y = f"{x_y_z[:-2]}" if len(x_y_z) > 3 else x_y_z

                    match = re.search("^.*_([0-9]{1,2}).{0,1}(aus|amc){0,1}.*(early|final).*$", task["slug"])
                    if match:
                        BATCH_ID = match.group(1)
                        BATCH_SUFFIX = match.group(2)
                        BATCH_SPIN = match.group(3)
                        if BATCH_SPIN == "early":
                            COMPOSE_LABEL = f"Update-{BATCH_ID}.0"
                        else:
                            COMPOSE_LABEL = f"Update-{BATCH_ID}.1"
                    else:
                        print(f"Failed to get batch info for {task['slug']}")
                        continue

                    # Search in Jira to see if task has been created
                    print(f"Search in Jira for rhel-{x_y_z} {BATCH_ID} {BATCH_SUFFIX} {BATCH_SPIN} compose, if no issue exists, file a new one.")
                    # issue = search_issue(f"project={milestone['jira_project']} and summary ~ 'rhel-{x_y_z} Batch {BATCH_ID} {BATCH_SPIN}' and issuetype != Sub-task and status != Closed AND status != Resolved")

                    # Create Jira tickets
                    # if not issue:
                        # print(f"Create Jira task for rhel-{x_y_z} Batch {BATCH_ID} {BATCH_SPIN} compose")
                    if BATCH_SUFFIX:
                        summary = f"[CMP] rhel-{x_y_z} Batch {BATCH_ID} {BATCH_SUFFIX} {BATCH_SPIN} compose"
                    else:
                        summary = f"[CMP] rhel-{x_y_z} Batch {BATCH_ID} {BATCH_SPIN} compose"
                    time.sleep(1)
                    issue = check_and_create_task_tracker(r, task, milestone, summary)

                    curl_param1 = f"Release_label_version={x_y_z}&Jira_ticket={issue}&Compose_label={COMPOSE_LABEL}"

                    print(datetime.datetime.strptime(task['date_start'], '%Y-%m-%d'))
                    print(datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d'))
                    # Send out pre-compose email the day before TRIGGER_DATE
                    if datetime.datetime.strptime(task['date_start'], '%Y-%m-%d') \
                        - datetime.timedelta(days=1) == \
                        datetime.datetime.strptime(TRIGGER_DATE, '%Y-%m-%d'):
                        print(f"INFO: Send pre-compose email for rhel-{x_y_z} "
                              f"{task['name']} one day before compose date.")
                        CURL_CMD = f"{curl_main}/{milestone['pre_compose_job']}/{curl_token}&{curl_param1}"
                        if BATCH_SUFFIX:
                            CURL_CMD += f"&BATCH_SUFFIX={BATCH_SUFFIX.upper()}"
                        CURL_CMD += "'"

                        try:
                            subprocess.check_call(CURL_CMD,shell=True)
                        except Exception as e:
                            raise_error(f'Executing error on pct_mr_tool.sh: {e}', issue)

                    elif task['date_start'] == TRIGGER_DATE:
                        print(f"INFO: Build compose for {release_short_name} "
                              f"{task['name']}")
                        compose_curl_cmds = []
                        if x_y == "7.9":
                            et_batch_id = rhelcmp_get_batch_id("1292", BATCH_ID, issue)
                            curl_param2 = f"{curl_main}/{milestone['rhel7_compose_job']}/{curl_token}&{curl_param1}&PRODUCT_STREAM={x_y}&Batch_id={et_batch_id}"
                            compose_curl_cmds = [f"{curl_param2}&PRODUCT=rhel'"]
                        else:
                            if x_y == "8.2":
                                BATCH_SUFFIX = "AMC"
                            compose_curl_cmds = [f"{curl_main}/{milestone['rhel8_compose_job']}/{curl_token}&{curl_param1}&PRODUCT_STREAM={x_y}&BATCH_SUFFIX={BATCH_SUFFIX.upper()}&CTS_ISSUE_ID={issue}'"]
                        for compose_curl_cmd in compose_curl_cmds:
                            try:
                                subprocess.check_call(compose_curl_cmd,shell=True)
                            except Exception as e:
                                raise_error(f'Executing error on pct_mr_tool.sh: {e}', issue)

            else:
                print(f"INFO: No compose build needed for {release_short_name}")

def rhelcmp_rhds_compose(r, milestone):
    release_short_name = r["shortname"]
    tasks = match_pp_tasks(release_short_name, milestone['milestone'])
    if len(tasks) > 0:
        for task in tasks:
            version_list = release_short_name.split('-')
            x_y_z = f"{version_list[1]}.{version_list[2]}"
            if (x_y_z not in task['path'][0]):
                continue
            print(f"Search in Jira for rhds {x_y_z} compose")
            issue = search_issue(f"project={milestone['jira_project']} and summary ~ 'Production compose for RHDS {x_y_z}' and issuetype != Sub-task and status != Closed AND status != Resolved")
             # Create Jira tickets
            if not issue:
                summary = f"Production compose for RHDS {x_y_z}"
                issue = check_and_create_task_tracker(r, task, milestone, summary)



if __name__ == '__main__':
    """
        release configuration phase1(new release configuration milestone on PP)
        new release config phase 2 scheduled after phase1 (enable osci gating on PP)
        post GA tasks (After the release GA date, we don't have message about this yet)
        EUS set up (we typically schedule this after GA. e.g: after 9.3 GA, we'll start
        configuration for 9.4 EUS)
        POST RC tasks (Nightly compose promoted as RC compose message on the PP)
        EOL configuration (scheduled start date for these tasks happen day after
        official EOL date of a lifecycle)
        e.g.
            https://pp.engineering.redhat.com/rhel-9-2.0.z/schedule/tasks/
            https://pp.engineering.redhat.com/rhel-9-3.0/schedule/tasks/
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--jira-token",
        default=JIRA_TOKEN,
        help=(
            "Token you are used to login"
        ),
    )
    parser.add_argument(
        "--gitlab-token",
        default=PCT_API_TOKEN,
        help=(
            "Token you are used to login"
        ),
    )
    parser.add_argument(
        "--jira-env",
        default="prod",
        help=(
            "JIRA instance you are using, by default it's prod, you can use stage"
        ),
    )
    parser.add_argument(
        "--ca-cert",
        default=SSL_CERT,
        help=(
            "The ca cert you use to verify the SSL."
        ),
    )
    parser.add_argument(
        "--team",
        help=(
            "Check which team's tasks you want to trigger, "
        ),
    )
    parser.add_argument(
        "--auto_merge",
        help=(
            "This is used for turning on auto merge to PCT repo, default value is False"
        ),
    )
    parser.add_argument("-d", "--dry-run", action="store_true")

    script_args = parser.parse_args()
    if script_args.dry_run:
        DRY_RUN = True
    if script_args.gitlab_token:
        PCT_API_TOKEN = script_args.gitlab_token
    if script_args.ca_cert:
        SSL_CERT = script_args.ca_cert
    if (not os.environ.get('REQUESTS_CA_BUNDLE')) and os.path.exists(SSL_CERT):
        session.verify = no_auth_session.verify = SSL_CERT
    if script_args.auto_merge:
        AUTO_MERGE = script_args.auto_merge
    JIRA_CONNECTION = JiraConnection(JIRA_URLs[script_args.jira_env], script_args.jira_token)

    team = script_args.team
    teams = ["cmp", "wf", "bld", "dst"]
    if team:
        if team not in teams:
            parser.error("The team should be cmp, wf, bld, dst, no team means all tasks")
        load_execute_tasks(team)
    else:
        for t in teams:
            load_execute_tasks(t)
