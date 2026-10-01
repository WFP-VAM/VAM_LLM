"""Deployment database aliases, resolved before services select a SQL dialect."""
def database_backend_alias(value):
    value = (value or '').strip().lower()
    return {'cloud_sql_postgres': 'postgres', 'cloud-sql-postgres': 'postgres'}.get(value, value)
