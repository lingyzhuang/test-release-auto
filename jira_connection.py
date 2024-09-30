from jira import JIRA


class JiraConnection:
    def __init__(self, jira_url, token):
        self.token = token
        self.connection = self.__connection(jira_url)

    def __connection(self, jira_url):
        headers = JIRA.DEFAULT_OPTIONS["headers"].copy()
        headers["Authorization"] = f"Bearer {self.token}"
        return JIRA(jira_url, options={"headers": headers})

    def issue(self, issue):
        issue_key = issue if isinstance(issue, str) else issue.key
        return self.connection.issue(issue_key)

    def comment(self, issue, text):
        return self.connection.add_comment(issue, text)

    def change_status(self, issue, status):
        return self.connection.transition_issue(issue, transition=status)
