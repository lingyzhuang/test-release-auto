import os

JIRA_URLs = {"prod": "https://issues.redhat.com",
             "stage": "https://issues.stage.redhat.com"
             }
PP_REST = "https://pp.engineering.redhat.com/api/latest/"
ET_URL = "https://errata.engineering.redhat.com/"
JIRA_USER = "rhelwf-automation"

# Keep the old OJA status "To Do" and add the new OJA status "New" here
# to forbid any rollback in the future
JIRA_ISSUE_NEW_STATES = ("To Do", "New")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
PCT_API_TOKEN = os.getenv("PCT_API_TOKEN")
RHELCMP_JENKINS_TOKEN = os.getenv("RHELCMP_JENKINS_TOKEN")
RHELCMP_PRE_JOB_TOKEN = os.getenv("RHELCMP_PRE_JOB_TOKEN")

# 37797 is the project id of product-configure-tool repo
PCT_MR_URL = "https://gitlab.cee.redhat.com/api/v4/projects/37797"
SSL_CERT = "/etc/pki/ca-trust/source/anchors/Current-IT-Root-CAs.pem"

RELEASE_METADATA_DIR = f"{os.path.dirname(os.path.abspath(__file__))}/release_info"
# A public server acccount to login to servers(e.g. Jenkins)
SERVICE_ACCOUNT = "rhel-release-automation"

JIRA_PROJECTS = {
    "wf": "RHELWF",
    "cmp": "RHELCMP",
    "dst": "RHELDST",
    "bld": "RHELBLD"
}
