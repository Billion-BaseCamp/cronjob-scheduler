"""Unit tests for outstanding demand parse / merge / diagnosis (no browser).

Fixtures mirror live portal payloads and cards; PAN replaced.
"""

from __future__ import annotations

from app.portal.actions.read_outstanding_demand import (
    demands_from_api_payload,
    journey_from_api,
    map_api_demand,
    map_dom_card,
    merge_demands,
    parse_rupee_amount,
)
from app.portal.notice_diagnosis import diagnose_notice, format_inr

PAN = "ABCDE1234F"


def _api_row(**overrides):
    row = {
        "itrAy": "2024",
        "aoResponseType": "Agree with demand",
        "din": "2025202437441932951T",
        "sectionCode": "143(3)",
        "modeOfService": None,
        "rectificationRights": "CPC",
        "dateOfDemandraised": 1771353000000,
        "dateOfServiceNotice": 1774377000000,
        "responseType": "Disagree with demand(Either in Full or Part)",
        "responseReason": "Disagree with demand",
        "aoResponseReason": "Demand outstanding is correct and collectible",
        "currentStatusDate": None,
        "orignalOutStDemandAmount": 1887130,
        "interestUs": "220(2)",
        "pan": PAN,
        "finalInterest": None,
        "currentStatus": "Pending Payment",
        "responseSubmitted": 1790188200000,
        "outstandingDemandAmountWOInterest": 1887130.0,
        "customerTrxId": 2078717821,
        "accruedInterest": 0,
        "transactionId": 2078717821,
        "outstandingDemandAmount": 1887130.0,
        "aoResponseDate": 1781721000000,
    }
    row.update(overrides)
    return row


API_PAYLOAD = {
    "header": {"formName": None},
    "messages": [{"code": "EF40003", "type": "INFO", "desc": "Record(s) fetched successfully."}],
    "errors": [],
    "demandList": [
        _api_row(),
        _api_row(
            itrAy="2007",
            aoResponseType="Neither Agree/Disagree with demand",
            din="2012200751110169060T",
            sectionCode="143(1)",
            rectificationRights=None,
            dateOfDemandraised=1214418600000,
            dateOfServiceNotice=1214418600000,
            aoResponseReason=None,
            currentStatusDate=1358447400000,
            orignalOutStDemandAmount=360230,
            outstandingDemandAmountWOInterest=360230,
            customerTrxId=75475484,
            accruedInterest=792440,
            transactionId=75475484,
            outstandingDemandAmount=1152670,
            aoResponseDate=None,
        ),
        _api_row(
            itrAy="2008",
            aoResponseType=None,
            din="2023200837004116980T",
            sectionCode="220(2)",
            rectificationRights=None,
            dateOfDemandraised=1705411001000,
            dateOfServiceNotice=None,
            responseType=None,
            responseReason=None,
            aoResponseReason=None,
            orignalOutStDemandAmount=390,
            currentStatus="EXTINGUISHED DEMAND",
            responseSubmitted=None,
            outstandingDemandAmountWOInterest=390,
            customerTrxId=1008836907,
            accruedInterest=None,
            transactionId=1008836907,
            outstandingDemandAmount=None,
            aoResponseDate=None,
        ),
        _api_row(
            orignalOutStDemandAmount=11624920,
            outstandingDemandAmountWOInterest=11624920,
            customerTrxId=2078717809,
            transactionId=2078717809,
            outstandingDemandAmount=11624920,
        ),
    ],
}

_EDIT = "mat-step-icon mat-step-icon-state-edit mat-step-icon-selected"
_DONE = "mat-step-icon mat-step-icon-state-done"
_DONE_SEL = "mat-step-icon mat-step-icon-state-done mat-step-icon-selected"


def _card(index, din, ay, amounts, fields, journey, buttons):
    return {
        "data_index": str(index),
        "header": {"Demand Reference No": din, "Assessment Year": ay},
        "amounts": amounts,
        "fields": fields,
        "journey": journey,
        "buttons": buttons,
    }


def _step(label, value, icon=_DONE, extinguished=False):
    return {"label": label, "value": value, "icon_class": icon, "extinguished": extinguished}


_FIELDS_143_3 = {
    "Section Code": "143(3)",
    "Rectification Rights": "CPC",
    "Mode of Service": "-",
    "Response Type": "Disagree with demand(Either in Full or Part)",
    "Applicable Act": "Income Tax Act 1961",
    "AO Response": "Demand outstanding is correct and collectible",
    "AO Response Date": "18-Jun-2026",
}
_JOURNEY_4 = [
    _step("Current Status", "Pending Payment", _EDIT),
    _step("Response Submitted", "24-Sep-2026"),
    _step("Date of Service of Notice", "25-Mar-2026"),
    _step("Date of Demand Raised", "18-Feb-2026"),
]

# Card order on the page differs from demandList order.
DOM_CARDS = [
    _card(
        0,
        "2025202437441932951T",
        "2024",
        {"Outstanding Demand Amount": "₹1887130"},
        _FIELDS_143_3,
        _JOURNEY_4,
        ["Pay Now", "Download", "Re-Submit Response"],
    ),
    _card(
        1,
        "2025202437441932951T",
        "2024",
        {"Outstanding Demand Amount": "₹11624920"},
        _FIELDS_143_3,
        _JOURNEY_4,
        ["Pay Now", "Download", "Re-Submit Response"],
    ),
    _card(
        2,
        "2023200837004116980T",
        "2008",
        {"Outstanding Demand Amount": "₹390"},
        {
            "Section Code": "220(2)",
            "Rectification Rights": "",
            "Mode of Service": "-",
            "Applicable Act": "Income Tax Act 1961",
        },
        [
            _step("Current Status", "EXTINGUISHED DEMAND", _DONE_SEL, extinguished=True),
            _step("Date of Demand Raised", "16-Jan-2024"),
        ],
        [],
    ),
    _card(
        3,
        "2012200751110169060T",
        "2007",
        {"Outstanding Demand Amount": "₹360230", "Accrued Interest": "₹792440"},
        {
            "Section Code": "143(1)",
            "Rectification Rights": "",
            "Mode of Service": "-",
            "Response Type": "Disagree with demand(Either in Full or Part)",
            "Applicable Act": "Income Tax Act 1961",
        },
        [
            _step("Current Status", "Pending Payment", _EDIT),
            _step("Response Submitted", "24-Sep-2026"),
            _step("Date of Demand Raised", "26-Jun-2008"),
        ],
        ["Pay Now", "Re-Submit Response"],
    ),
]


def test_parse_rupee_amount() -> None:
    assert parse_rupee_amount("₹4190703") == 4190703
    assert parse_rupee_amount(" ₹0 ") == 0
    assert parse_rupee_amount("₹2,73,042") == 273042
    assert parse_rupee_amount(None) is None
    assert parse_rupee_amount("-") is None


def test_map_api_demand_uses_principal_and_ist_dates() -> None:
    row = map_api_demand(API_PAYLOAD["demandList"][1])
    assert row["outstanding_demand_amount"] == 360230
    assert row["outstanding_with_interest"] == 1152670
    assert row["accrued_interest"] == 792440
    assert row["transaction_id"] == "75475484"
    assert row["assessment_year"] == "2007"
    assert row["notice_section"] == "143(1)"
    assert row["date_of_demand_raised"] == "2008-06-26"
    assert row["response_submitted_on"] == "2026-09-24"


def test_journey_from_api_omits_null_steps() -> None:
    four = journey_from_api(API_PAYLOAD["demandList"][0])
    assert [s["label"] for s in four] == [
        "Current Status",
        "Response Submitted",
        "Date of Service of Notice",
        "Date of Demand Raised",
    ]
    assert four[0] == {"label": "Current Status", "value": "Pending Payment", "state": "in_progress"}
    assert four[2]["value"] == "2026-03-25"
    assert four[3]["value"] == "2026-02-18"

    two = journey_from_api(API_PAYLOAD["demandList"][2])
    assert [s["label"] for s in two] == ["Current Status", "Date of Demand Raised"]
    assert two[0]["state"] == "done"


def test_map_dom_card_fields_and_actions() -> None:
    row = map_dom_card(DOM_CARDS[0])
    assert row["din"] == "2025202437441932951T"
    assert row["assessment_year"] == "2024"
    assert row["outstanding_demand_amount"] == 1887130
    assert row["mode_of_service"] is None
    assert row["applicable_act"] == "Income Tax Act 1961"
    assert row["ao_response_date"] == "2026-06-18"
    assert row["date_of_service_of_notice"] == "2026-03-25"
    assert row["actions"] == {
        "pay_now": True,
        "download": True,
        "submit_response": False,
        "resubmit_response": True,
    }
    assert row["has_submit_response"] is False
    assert row["journey"][0]["state"] == "in_progress"
    assert row["journey"][1] == {"label": "Response Submitted", "value": "2026-09-24", "state": "done"}


def test_map_dom_card_extinguished() -> None:
    row = map_dom_card(DOM_CARDS[2])
    assert row["is_extinguished"] is True
    assert row["rectification_rights"] is None
    assert row["actions"]["pay_now"] is False
    assert len(row["journey"]) == 2


def test_map_dom_card_no_response_submit_cta() -> None:
    row = map_dom_card(
        _card(
            0,
            "2016201537046404681T",
            "2015",
            {"Outstanding Demand Amount": "₹99102", "Accrued Interest": "₹273042"},
            {
                "Section Code": "271(1)(c)",
                "Rectification Rights": "Assessing Officer",
                "Mode of Service": "-",
                "Response Type": "No Response",
                "Applicable Act": "Income Tax Act 1961",
            },
            [
                _step("Current Status", "Pending Payment", _EDIT),
                _step("Date of Demand Raised", "20-Sep-2022"),
            ],
            ["Pay Now", "Download", "Submit Response"],
        )
    )
    assert row["has_submit_response"] is True
    assert row["actions"]["resubmit_response"] is False
    summary = diagnose_notice(row)
    assert "Submit Response pending" in summary
    assert "271(1)(c)" in summary


def test_map_dom_card_final_interest() -> None:
    row = map_dom_card(
        _card(
            1,
            "2022202137076111111T",
            "2021",
            {"Outstanding Demand Amount": "₹0", "Final Interest": "₹36"},
            {"Section Code": "154", "Mode of Service": "Email & Post"},
            [],
            ["Pay Now"],
        )
    )
    assert row["outstanding_demand_amount"] == 0
    assert row["final_interest"] == 36
    assert row["accrued_interest"] is None
    assert row["mode_of_service"] == "Email & Post"


def test_merge_matches_same_din_by_amount_despite_order() -> None:
    api_rows = demands_from_api_payload(API_PAYLOAD)
    dom_rows = [map_dom_card(c) for c in DOM_CARDS]
    merged = merge_demands(api_rows, dom_rows)

    assert len(merged) == 4
    by_trx = {row["transaction_id"]: row for row in merged}
    assert set(by_trx) == {"2078717821", "75475484", "1008836907", "2078717809"}
    assert by_trx["2078717809"]["outstanding_demand_amount"] == 11624920
    assert all(row["journey_source"] == "dom" for row in merged)
    assert all(row["applicable_act"] == "Income Tax Act 1961" for row in merged)
    assert by_trx["1008836907"]["is_extinguished"] is True
    assert by_trx["75475484"]["actions"]["resubmit_response"] is True


def test_merge_keeps_unmatched_rows_from_both_sides() -> None:
    api_rows = demands_from_api_payload({"demandList": [API_PAYLOAD["demandList"][2]]})
    dom_rows = [map_dom_card(DOM_CARDS[3])]
    merged = merge_demands(api_rows, dom_rows)
    assert [row["journey_source"] for row in merged] == ["api", "dom"]


def test_demands_from_api_payload_empty_and_bad() -> None:
    assert demands_from_api_payload({"demandList": []}) == []
    assert demands_from_api_payload({"messages": []}) == []
    assert demands_from_api_payload(None) == []


def test_format_inr() -> None:
    assert format_inr(4190703) == "₹41,90,703"
    assert format_inr(390) == "₹390"
    assert format_inr(11624920) == "₹1,16,24,920"


def test_diagnose_demand_is_correct_and_extinguished() -> None:
    correct = diagnose_notice(
        map_dom_card(
            _card(
                0,
                "2025201840425213492T",
                "2018",
                {"Outstanding Demand Amount": "₹4190703", "Accrued Interest": "₹251442"},
                {
                    "Section Code": "154",
                    "Rectification Rights": "CPC",
                    "Mode of Service": "-",
                    "Response Type": "Demand is correct",
                    "Applicable Act": "Income Tax Act 1961",
                    "AO Response": "Demand outstanding is correct and collectible",
                    "AO Response Date": "15-Jun-2026",
                },
                [
                    _step("Current Status", "Pending Payment", _EDIT),
                    _step("Response Submitted", "04-Aug-2026"),
                    _step("Date of Demand Raised", "11-Mar-2026"),
                ],
                ["Pay Now", "Download"],
            )
        )
    )
    assert "₹41,90,703" in correct
    assert "₹2,51,442" in correct
    assert "Demand is correct" in correct
    assert "Re-Submit" not in correct

    extinguished = diagnose_notice(map_dom_card(DOM_CARDS[2]))
    assert "extinguished" in extinguished
    assert "no action" in extinguished
