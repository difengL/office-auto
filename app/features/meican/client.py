"""美餐 v3 API 客户端。

两套体系说明：
- v3 REST: meican.com/forward/api/v2.1/*，必须同时带 client_id/client_secret query 参数
- v4 GraphQL: gateway.meican.com/graphql，用于手机验证码登录（能换出 v3 可用的 token）
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx

V3_BASE = "https://www.meican.com/forward/api/v2.1"
GRAPHQL_URL = "https://gateway.meican.com/graphql"

# 公开的前端常量（从 bundle 提取）
V3_CLIENT_ID = "Xqr8w0Uk4ciodqfPwjhav5rdxTaYepD"
V3_CLIENT_SECRET = "vD11O6xI9bG3kqYRu9OyPAHkRGxLh4E"
V4_CLIENT_ID = "WYAiIJZPc8e21UHcKHVUeVo2SpNVrni"
V4_CLIENT_SECRET = "WbRV03U0MyQzRhXrvXhyopkavkIRaBg"
WEB_PC_APP = "meican/web-pc (prod;4.97.4;sys;main)"
REGULATION_IMG_HEADER = {"x-mc-demeterfe-image": "oops-4841-regulation"}


class MeicanError(Exception):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


class MeicanClient:
    def __init__(self, access_token: str | None = None, timeout: float = 25.0):
        self.access_token = access_token
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    # ---------- 登录（v4 GraphQL）----------

    def send_sms_code(self, phone: str) -> None:
        """发送短信验证码（v4 通道）。注意：接口可能静默限流，返回 OK 但短信不发。"""
        self._gql(
            "GetPhoneVerificationCode",
            "mutation GetPhoneVerificationCode($input: UserCenterSsoV2PhoneVerificationCodeRequest!)"
            " { getPhoneVerificationCode(input: $input) }",
            {"input": {"phone": phone, "idpType": "UserCenterIDP"}},
        )

    def login_with_sms(self, phone: str, code: str) -> dict:
        """验证码登录，返回 {ticket, signature, users}。"""
        data = self._gql(
            "LoginByAuthWay",
            """mutation LoginByAuthWay($input: LoginByAuthWayInput!) {
                 loginByAuthWay(input: $input) {
                   data {
                     signature
                     ticket
                     userList { id name phone accountVersion snowflakeId type }
                     clientMemberList { id clientId name accountVersion }
                   }
                   meta { code msg }
                 }
               }""",
            {
                "input": {
                    "authMethod": "PhoneVerificationCode",
                    "phone": phone,
                    "verificationCode": code,
                    "createAccountIfNotExist": True,
                    "createAccountScene": "UserSelfRegistration",
                }
            },
        )
        node = (data.get("loginByAuthWay") or {}).get("data") or {}
        if not node.get("ticket"):
            raise MeicanError("登录未返回 ticket", payload=data)
        return node

    def choose_account(self, ticket: str, signature: str, snowflake_id: str | None = None,
                       client_id: str | None = None, client_member_id: str | None = None) -> dict:
        """用 ticket 换 token。"""
        inp: dict[str, Any] = {"ticket": ticket, "signature": signature}
        if snowflake_id:
            inp["snowflakeId"] = snowflake_id
        if client_id:
            inp["clientId"] = client_id
        if client_member_id:
            inp["clientMemberId"] = client_member_id

        data = self._gql(
            "ChooseAccountLogin",
            """mutation ChooseAccountLogin($input: ChooseAccountLoginInput!) {
                 chooseAccountLogin(input: $input) {
                   token { accessToken refreshToken expiry refreshTokenExpiry tokenType }
                 }
               }""",
            {"input": inp},
        )
        token = (data.get("chooseAccountLogin") or {}).get("token")
        if not token:
            raise MeicanError("换取 token 失败", payload=data)
        self.access_token = token["accessToken"]
        return token

    def _gql(self, op: str, query: str, variables: dict) -> dict:
        body = {"operationName": op, "query": query, "variables": variables}
        headers = {
            "content-type": "application/json",
            "clientid": V4_CLIENT_ID,
            "clientsecret": V4_CLIENT_SECRET,
            "x-mc-app": WEB_PC_APP,
            "accept-language": "zh",
        }
        r = self._client.post(f"{GRAPHQL_URL}?op={op}", json=body, headers=headers)
        try:
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise MeicanError(f"GraphQL 返回非 JSON: {r.text[:200]}", r.status_code) from exc
        if payload.get("errors"):
            msg = payload["errors"][0].get("message", "未知错误")
            raise MeicanError(msg, r.status_code, payload)
        return payload.get("data") or {}

    # ---------- v3 REST ----------

    def _v3_get(self, path: str, params: dict | None = None, extra_headers: dict | None = None):
        headers = {"authorization": f"bearer {self.access_token}", "accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        query = dict(params or {})
        query["client_id"] = V3_CLIENT_ID
        query["client_secret"] = V3_CLIENT_SECRET
        r = self._client.get(f"{V3_BASE}/{path}", params=query, headers=headers)
        return self._parse_v3(r)

    def _v3_post(self, path: str, data: dict | None = None, json_body: bool = False,
                 extra_headers: dict | None = None):
        headers = {"authorization": f"bearer {self.access_token}", "accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        params = {"client_id": V3_CLIENT_ID, "client_secret": V3_CLIENT_SECRET}
        if json_body:
            r = self._client.post(f"{V3_BASE}/{path}", params=params, json=data or {}, headers=headers)
        else:
            r = self._client.post(f"{V3_BASE}/{path}", params=params, data=data or {}, headers=headers)
        return self._parse_v3(r)

    @staticmethod
    def _parse_v3(r: httpx.Response):
        if r.status_code == 401:
            raise MeicanError("token 已失效", 401)
        try:
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise MeicanError(f"接口返回非 JSON (HTTP {r.status_code}): {r.text[:200]}", r.status_code) from exc
        if r.status_code >= 400:
            msg = payload.get("error_description") or payload.get("error") or f"HTTP {r.status_code}"
            raise MeicanError(msg, r.status_code, payload)
        return payload

    # ---------- 业务接口 ----------

    def account(self) -> dict:
        return self._v3_get("accounts/show").get("data") or {}

    def calendar_items(self, start: date, end: date, with_order: bool = True) -> list[dict]:
        """返回 [{date, calendarItemList:[...]}]"""
        payload = self._v3_get(
            "calendarItems/list",
            {
                "withOrderDetail": "true" if with_order else "false",
                "beginDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
        )
        return payload.get("dateList") or []

    def restaurants(self, tab_unique_id: str, target_time: str) -> list[dict]:
        payload = self._v3_get(
            "restaurants/list",
            {"tabUniqueId": tab_unique_id, "targetTime": target_time},
            extra_headers=REGULATION_IMG_HEADER,
        )
        return payload.get("restaurantList") or []

    def restaurant_menu(self, restaurant_unique_id: str, tab_unique_id: str, target_time: str) -> dict:
        return self._v3_get(
            "restaurants/show",
            {
                "tabUniqueId": tab_unique_id,
                "targetTime": target_time,
                "restaurantUniqueId": restaurant_unique_id,
            },
            extra_headers=REGULATION_IMG_HEADER,
        )

    def cart_query(self, tab_uuid: str, close_time: str) -> dict:
        return self._v3_post("cart/query", {"tabUUID": tab_uuid, "closeTime": close_time})

    def cart_update(self, tab_uuid: str, tab_name: str, close_time: str,
                    dishes: list[dict], corp_name: str, operative_date: str) -> dict:
        return self._v3_post(
            "cart/update",
            {
                "tabUUID": tab_uuid,
                "tabName": tab_name,
                "corpName": corp_name,
                "operativeDate": operative_date,
                "dishes": dishes,
            },
            json_body=True,
        )

    def place_order(self, tab_unique_id: str, target_time: str, dishes: list[dict],
                    address_unique_id: str, remarks: str | None = None) -> dict:
        """下单。dishes 形如 [{"dishId": 123, "count": 1}]"""
        order = json.dumps([{"dishId": d["dishId"], "count": d.get("count", 1)} for d in dishes],
                           ensure_ascii=False)
        data = {
            "tabUniqueId": tab_unique_id,
            "order": order,
            "remarks": json.dumps(remarks, ensure_ascii=False) if remarks else "null",
            "targetTime": target_time,
            "userAddressUniqueId": address_unique_id,
            "corpAddressUniqueId": address_unique_id,
            "corpAddressRemark": "",
        }
        payload = self._v3_post("orders/add", data)
        if payload.get("resultDescription") not in (None, "SUCCESS", "SUCCESSFUL"):
            raise MeicanError(payload.get("resultDescription") or "下单失败", payload=payload)
        return payload

    def delete_order(self, order_unique_id: str) -> dict:
        return self._v3_post("orders/delete", {"uniqueId": order_unique_id})
