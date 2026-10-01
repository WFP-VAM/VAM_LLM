"""Cloud deployment aliases terminate in the shared layer, including migrations."""
import pytest
from app.services.price_cache.config import load_price_cache_config
from app.services.price_cache.migrations import _normalize_dialect

@pytest.mark.parametrize('alias',['cloud_sql_postgres','cloud-sql-postgres',' CLOUD_SQL_POSTGRES '])
def test_deployment_aliases_reach_services_only_as_postgres(alias):
    config=load_price_cache_config({'PRICE_CACHE_BACKEND':alias,'PRICE_CACHE_DATABASE_URL':'postgresql://test:unused@localhost/test'})
    assert config.backend==_normalize_dialect(alias)=='postgres'
    assert not any(name.startswith('gcp_') for name in config.__dict__)
    assert config.pool_recycle_seconds==240 and config.pool_timeout_seconds==30
    assert config.connect_timeout_seconds==10 and config.tcp_keepalives_count==3
