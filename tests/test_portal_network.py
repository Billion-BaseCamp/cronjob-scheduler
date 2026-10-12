"""Portal network log lines carry no client PAN, mobile or email. No browser."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace

from app.portal import network
from app.portal.network import login_api_error, mask_pans, summarize_body

_PAN = "ABCDE1234F"
_MOBILE = base64.b64encode(b"9000000000").decode()
_EMAIL = base64.b64encode(b"client@example.com").decode()


def _login_body(messages: list[dict]) -> str:
    return json.dumps(
        {
            "header": {"formName": None},
            "messages": messages,
            "errors": [],
            "reqId": "FOS000000000000",
            "entity": _PAN,
            "entityType": "PAN",
            "role": "IN",
            "mobileNo": _MOBILE,
            "email": _EMAIL,
            "imgByte": "",
        }
    )


def _assert_no_client_data(line: str) -> None:
    assert _PAN not in line
    assert _MOBILE not in line
    assert _EMAIL not in line
    assert "FOS000000000000" not in line


def test_login_success_logs_only_the_message_code() -> None:
    line = summarize_body(
        _login_body([{"code": "EF00000", "type": "INFO", "desc": "OK", "fieldName": None}])
    )
    assert line == "EF00000 OK"
    _assert_no_client_data(line)


def test_login_error_and_errors_list_are_kept() -> None:
    body = json.loads(
        _login_body(
            [{"code": "EF500023", "type": "ERROR", "desc": "Request is not authenticated"}]
        )
    )
    body["errors"] = [{"code": "EF40000", "desc": f"Invalid password for {_PAN}"}]
    line = summarize_body(json.dumps(body))
    assert line == "EF500023 Request is not authenticated; EF40000 Invalid password for [PAN]"
    _assert_no_client_data(line)


def test_bodies_without_messages_are_not_logged() -> None:
    assert summarize_body("") == "(empty body)"
    assert summarize_body("<html>PAN ABCDE1234F</html>") == "(non-JSON body, 27 chars)"
    assert summarize_body(json.dumps([_PAN])) == "(JSON body)"
    assert summarize_body(json.dumps({"entity": _PAN, "email": _EMAIL})) == "(no portal messages)"


def test_mask_pans() -> None:
    assert mask_pans(f"/api/pan/{_PAN}/status") == "/api/pan/[PAN]/status"
    assert mask_pans("abcde1234f") == "[PAN]"
    assert mask_pans("EF00000 OK") == "EF00000 OK"


def _response(url: str, status: int, body: str, method: str = "POST"):
    async def text() -> str:
        return body

    async def as_json():
        return json.loads(body)

    return SimpleNamespace(
        url=url,
        status=status,
        text=text,
        json=as_json,
        request=SimpleNamespace(resource_type="xhr", method=method),
    )


def test_net_log_line_has_no_client_data(caplog) -> None:
    caplog.set_level(logging.INFO, logger=network.__name__)
    login = _response(
        "https://eportal.incometax.gov.in/iec/loginapi/login",
        200,
        _login_body([{"code": "EF00000", "type": "INFO", "desc": "OK"}]),
    )
    failed = _response(
        f"https://eportal.incometax.gov.in/iec/api/{_PAN}/details", 500, "oops", "GET"
    )
    asyncio.run(network._on_response(login))
    asyncio.run(network._on_response(failed))

    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        "NET 200 POST https://eportal.incometax.gov.in/iec/loginapi/login EF00000 OK",
        "NET 500 GET https://eportal.incometax.gov.in/iec/api/[PAN]/details "
        "(non-JSON body, 4 chars)",
    ]
    for line in lines:
        _assert_no_client_data(line)


def test_login_api_error_masks_pan() -> None:
    body = _login_body([{"code": "EF1", "type": "ERROR", "desc": f"PAN {_PAN} is locked"}])
    assert asyncio.run(login_api_error(_response("x", 200, body))) == "EF1 PAN [PAN] is locked"
