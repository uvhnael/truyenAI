"""Regression assertions: keep failures visible for production implementation fixes."""

from app.agent.workflow import run_story


async def test_edit_response_reports_committed_updated_timestamp(client, mock_mode):
    result = await client.post(
        "/api/projects",
        json={
            "prompt": "Một câu chuyện tiếng Việt đủ dài.",
            "options": {"chapter_count": 1},
        },
    )
    assert result.status_code == 201
    pid = result.json()["id"]
    await run_story(pid, 1)
    url = f"/api/projects/{pid}/chapters/1"
    before = (await client.get(url)).json()
    response = await client.put(
        url,
        json={
            "title": "Tiêu đề mới",
            "content": "Nội dung thay đổi hoàn toàn.",
            "version": before["version"],
        },
    )
    assert response.status_code == 200
    committed = (await client.get(url)).json()
    assert response.json()["updated_at"] == committed["updated_at"], (
        "PUT must serialize the persisted updated_at, not the pre-flush timestamp"
    )
