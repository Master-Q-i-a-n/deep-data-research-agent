import hashlib
import re
from types import SimpleNamespace

import httpx
import pytest

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.database import repository as database
from deep_data_research_agent.skill_system import management


def matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(document, part) for part in expected):
                return False
            continue
        value = document
        exists = True
        for part in key.split("."):
            try:
                value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError):
                exists, value = False, None
                break
        if isinstance(expected, dict):
            if "$exists" in expected and exists != expected["$exists"]:
                return False
            if "$regex" in expected and not re.search(expected["$regex"], value or ""):
                return False
        elif value != expected:
            return False
    return True


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query, projection, **kwargs):
        assert projection == {"key": 1, "namespace": 1}

        async def rows():
            for document in self.documents:
                if matches(document, query):
                    yield document

        return rows()

    async def delete_many(self, query):
        remaining = [doc for doc in self.documents if not matches(doc, query)]
        count = len(self.documents) - len(remaining)
        self.documents = remaining
        return SimpleNamespace(deleted_count=count)


@pytest.fixture
def collection(monkeypatch):
    owner = hashlib.sha256(b"user-a").hexdigest()
    docs = [
        {"namespace": [identity, category, agent], "key": key}
        for identity, category, agent, key in [
            (owner, "skills", "supervisor", "/active/demo/SKILL.md"),
            (owner, "skills", "supervisor", "/active/demo/scripts/run.py"),
            (owner, "skills", "supervisor", "/manifests/demo.json"),
            (owner, "skills", "data-analyst", "/active/demo/SKILL.md"),
            (owner, "skills", "custom-agent", "/manifests/demo.json"),
            (owner, "skills", "supervisor", "/active/demo-extra/SKILL.md"),
            (owner, "skills", "supervisor", "/active/broken/reference.txt"),
            (owner, "skills", "supervisor", "/manifests/orphan.json"),
            (owner, "skills", "supervisor", "/skill-manage/demo/SKILL.md"),
            (owner, "memories", "supervisor", "/active/demo/SKILL.md"),
            ("public", "skills", "supervisor", "/active/demo/SKILL.md"),
            ("another-user", "skills", "supervisor", "/active/demo/SKILL.md"),
        ]
    ]
    result = Collection(docs)
    monkeypatch.setattr(management, "_collection", lambda: result)

    async def resolve(token):
        return (
            database.UserRecord("user-a", "Alice", False) if token == "valid" else None
        )

    monkeypatch.setattr(database, "resolve_login_session", resolve)
    return result


@pytest.mark.asyncio
async def test_list_and_delete_private_assignments(collection):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app),
        base_url="http://test",
        headers={"Authorization": "Bearer valid"},
    ) as client:
        response = await client.get("/skills/user")
        assert response.status_code == 200
        assert response.json()["skills"] == [
            {"name": "broken", "agents": ["supervisor"]},
            {"name": "demo", "agents": ["custom-agent", "data-analyst", "supervisor"]},
            {"name": "demo-extra", "agents": ["supervisor"]},
            {"name": "orphan", "agents": ["supervisor"]},
        ]
        protected = collection.documents[5:]
        response = await client.delete("/skills/user/demo")
        assert response.json() == {"status": "deleted", "deleted_count": 5}
        assert collection.documents == protected
        assert (await client.delete("/skills/user/demo")).json()["deleted_count"] == 0
        for name in ("broken", "orphan"):
            assert (await client.delete(f"/skills/user/{name}")).json()[
                "deleted_count"
            ] == 1


@pytest.mark.asyncio
async def test_empty_private_skill_list(collection):
    collection.documents = [
        doc for doc in collection.documents if doc["namespace"][0] == "public"
    ]
    assert (
        await management.list_user_skills(hashlib.sha256(b"user-a").hexdigest()) == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path", [("GET", "/skills/user"), ("DELETE", "/skills/user/demo")]
)
@pytest.mark.parametrize("token", [None, "expired"])
async def test_skill_api_requires_auth(collection, method, path, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app), base_url="http://test"
    ) as client:
        response = await client.request(
            method, path, headers={"Authorization": f"Bearer {token}"} if token else {}
        )
        assert response.status_code == 401
        assert len(collection.documents) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Bad", "a--b", "a.b", "a" * 65])
async def test_skill_delete_rejects_invalid_names(collection, name):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app),
        base_url="http://test",
        headers={"Authorization": "Bearer valid"},
    ) as client:
        assert (await client.delete(f"/skills/user/{name}")).status_code == 422
        assert len(collection.documents) == 12


@pytest.mark.asyncio
async def test_skill_api_hides_database_errors(collection, monkeypatch):
    def unavailable():
        raise RuntimeError("mongodb://secret-host")

    monkeypatch.setattr(management, "_collection", unavailable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app),
        base_url="http://test",
        headers={"Authorization": "Bearer valid"},
    ) as client:
        for method, path in [("GET", "/skills/user"), ("DELETE", "/skills/user/demo")]:
            response = await client.request(method, path)
            assert response.status_code == 503
            assert "secret-host" not in response.text
