"""Dispatch one Cloud Run task, without waiting for the execution to finish."""
class CloudJobs:
    def __init__(self, settings):
        self.settings = settings

    def launch(self, run_id, operation_id):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        s = self.settings
        url = f'https://run.googleapis.com/v2/projects/{s.project}/locations/{s.job_region}/jobs/{s.job}:run'
        # A timeout here is ambiguous. Never automatically dispatch again.
        with AuthorizedSession(credentials) as session:
            response = session.post(url, json={'overrides': {'taskCount': 1, 'containerOverrides': [
                {'args': ['-m', 'app.services.seasonal_outlook.worker', '--run', run_id, '--operation', operation_id]}]}}, timeout=60)
            response.raise_for_status()
            return response.json()['name']
