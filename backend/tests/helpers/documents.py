from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app

def client_for(snapshot, tmp_path):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    return ProjectedClient(create_app(db)), db
