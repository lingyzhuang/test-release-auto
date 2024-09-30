#!/bin/bash
# Script to create MR.
# Usage:
#     rhelwf_scripts/utils/pct_mr_tool.sh RELEASE_TASK_TYPE COMMIT_BRANCH
# RELEASE_TASKTYPE is one of: phase1, phase2, extention, post_rc, post_ga, eol, async

set -eu

trap "rm -rf /tmp/product-configure-tool" QUIT TERM INT HUP EXIT

PCT=https://gitlab-ci-token:"$PCT_API_TOKEN"@gitlab.cee.redhat.com/rhelwfautomation/product-configure-tool.git

git clone "$PCT" /tmp/product-configure-tool
cd /tmp/product-configure-tool || exit

git config --global user.email "exd-guild-rhel-workflow-automation-admin@redhat.com"
git config --global user.name "RHEWLF Automation"


# In different case calling with different parameters
release_task="$1"
commit_message=""
commit_branch=$2

git switch --create "$commit_branch"

case $release_task in
    "phase1")
        echo "New release phase1 task"
        ./scripts/create_variable_file.py --version "$3" --ship-date "$4"
        commit_message="New Configuration $3 with ship date $4"
        ;;
    "phase2")
        echo "New release phase2 task"
        echo "NOT IMPLEMENTED YET"
        exit 1
        ;;
    "extention")
        echo "New release extentions task"
        ;;
    "post_rc")
        echo "Post rc task"
        ;;
    "post_ga")
        echo "Post ga task"
        ;;
    "eol")
        echo "The EOL task"
        ;;
    "async")
        echo "ASYNC creating task"
        ;;
    *)
        echo "No task matches"
        exit 1
        ;;
esac

git add .
git commit -m "$commit_message"
git push origin "$commit_branch"
echo "pushed local change to $commit_branch and will clean up tmp dir"

