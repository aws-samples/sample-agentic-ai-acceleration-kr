# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""비용 집계의 **정답 정의** — 단일 진실원 (DEVLOG §59).

대시보드·budget·analytics·my·chat 어디서 봐도 같은 숫자가 나오도록, "운영 비용
집계"의 기준을 한 곳에 못박는다:

  1. **SUCCESS 만 합산** — ERROR/TIMEOUT 호출은 비용에서 제외(유효 사용량 관점).
     (실측: 이번 달 ERROR 26건 $2.32 + TIMEOUT 17건 $1.18 이 실패 호출에도 비용으로
      쌓여 있어, status 필터 없으면 Top 사용자/팀이 부풀려졌었다.)
  2. **KST(Asia/Seoul) 월 경계** — 한국 운영 자산이므로 캘린더 경계는 KST 기준.
     timestamptz 에 to_char 를 그냥 쓰면 DB 세션 TZ(UTC)로 잘려 KST 6/1 0~9시
     호출이 5월로 새는 9시간 오차가 생긴다 → 명시적 KST 변환으로 강제.

⚠️ 이 필터는 **비용/사용량 표시용**에만 쓴다. 에러율·모니터링처럼 ERROR/TIMEOUT 을
세야 하는 쿼리에는 success_only=False 로 쓰거나 쓰지 않는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, func

from app.core.exceptions import ValidationError
from app.models.usage import UsageLog, UsageStatus

# KST = UTC+9 고정 오프셋. 한국은 서머타임이 없어(1988 서울올림픽 이후) 오프셋이
# 불변이므로, 경계 계산에 zoneinfo 대신 고정 오프셋을 쓸 수 있다. 이 전제가 깨지면
# (=DST 도입) 아래 period_to_utc_range 는 zoneinfo 기반으로 바꿔야 한다.
KST = timezone(timedelta(hours=9))

# period 의 **모양**을 고정한다 — 정확히 4자리 연 + 2자리 월. `int()` 는 숫자인지만
# 보므로 '26-08'(연 26) 이나 '2026-8' 을 통과시킨다(아래 참조).
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")

# 운영상 의미 있는 연 범위. 이 자산의 데이터는 2026 년부터 존재하고, 상한은
# `datetime(year + 1, ...)` 이 MAXYEAR(9999)를 넘지 않도록 하는 역할도 겸한다
# ('9999-12' 는 이 검사가 없으면 end 경계 계산에서 맨 ValueError → 500 이 된다).
_PERIOD_MIN_YEAR = 2000
_PERIOD_MAX_YEAR = 2100


def current_kst_period() -> str:
    """"지금"이 속한 KST 월을 'YYYY-MM' 으로. 기본 period 의 **단일 진실원**.

    ⚠️ `date.today()` / `datetime.now()` 로 기본 기간을 정하면 **프로세스 로컬 TZ**,
    즉 pod 에서는 UTC 를 따른다. 데이터는 KST 월로 버킷되므로(§59) 매월 1일
    KST 00:00~09:00 의 9시간 동안 기본 기간만 지난달이 되어, 사용자에게는
    "새 달이 됐는데 지표가 지난달"로 보인다. 반대로 월말 마지막 9시간엔 이미
    다음 달로 넘어간 것처럼 보인다.

    이 헬퍼가 생긴 이유: 같은 두 줄(`now = date.today()` + f-string)이 5곳에
    복사돼 있었고, E-1 수정 때 dashboard/productivity 2곳만 KST 로 바뀌어
    **나머지 3곳(my.budget/my.usage/analytics.models/budgets.allocation)이
    UTC 로 남았다**. 화면마다 기본 월이 갈리는 건 복붙이 원인이므로 한 곳으로 모은다.
    """
    now_kst = datetime.now(KST)
    return f"{now_kst.year}-{now_kst.month:02d}"


def kst_month_expr() -> ColumnElement:
    """usage_logs.requested_at 을 KST 로 변환한 'YYYY-MM' 문자열 식.

    UI/호출부가 period(YYYY-MM)를 KST 기준으로 넘긴다는 전제.

    ⚠️ **GROUP BY / SELECT 투영에만** 쓸 것. WHERE 절에서 `== period` 로 쓰면
    컬럼을 함수로 감싸 non-sargable 이 되어 인덱스를 못 쓴다(아래 참조).
    기간 필터가 필요하면 `cost_period_filter()` / `period_to_utc_range()` 를 쓴다.
    """
    return func.to_char(func.timezone("Asia/Seoul", UsageLog.requested_at), "YYYY-MM")


def period_to_utc_range(period: str) -> tuple[datetime, datetime]:
    """KST 기준 'YYYY-MM' → UTC 반개구간 [start, end).

    변환을 **컬럼 쪽이 아니라 파라미터 쪽**에서 수행하는 것이 핵심이다. 예:
      period='2026-08' → [2026-07-31T15:00:00Z, 2026-08-31T15:00:00Z)
    (KST 8/1 00:00 = UTC 7/31 15:00)

    반개구간(>= start, < end)이라 경계 순간이 중복/누락되지 않는다. BETWEEN 은
    양끝 포함이라 월 경계에서 1마이크로초 겹침이 생기므로 쓰지 않는다.

    ⚠️ 형식이 틀리면 `ValidationError` → **400** 이다(main.py 의 핸들러가 변환).
    과거 구현은 `to_char(...) == period` 였어서 'YYYY-MM' 이 아닌 값('7d' 등)이 와도
    **조용히 0건**을 반환했다 — 즉 잘못된 질문에 "데이터 없음"으로 답해, 오타 난
    대시보드가 빈 화면으로 보일 뿐 원인을 알 수 없었다. 경계 계산을 파이썬으로 옮긴
    뒤로는 `int()` 가 그대로 터져 500 이 됐다(dev E2E 에서 실측). 둘 다 틀렸다 —
    조용한 거짓말과 스택트레이스 사이의 정답은 명시적 400 이다.
    admin-ui(대시보드)는 데이터가 있는 'YYYY-MM' 월만 넘기므로
    정상 경로에는 영향이 없고, 이 검증은 API 를 직접 호출하는 경우에만 걸린다.

    ⚠️ **모양을 정규식으로 고정하는 이유**(prod E2E 실측). 과거 구현은
    `split('-',1)` + `int()` 였다. `int()` 는 "숫자인가"만 보고 "몇 자리인가"는 보지
    않으므로 다음이 전부 통과했다:

        '26-08'   → 서기 26 년 8월  → [0026-07-31T15:00Z, 0026-08-31T15:00Z)
        '0026-08' → 같음
        '2026-8'  → 월 1자리도 통과

    통과하면 쿼리는 정상 실행되고 그 구간에 행이 없으니 **HTTP 200 + 전부 0** 이
    된다. 즉 잘못된 입력이 400 이 아니라 "이번 달 사용량 0" 이라는 그럴듯한
    거짓말로 돌아온다 — 이 함수가 애초에 없애려던 실패 양상 그대로다.
    자리수는 값의 범위가 아니라 **형식 불변식**이라 `int()` 로는 표현할 수 없다.

    연 범위 검사도 같은 이유로 필요하다. '9999-12' 는 형식이 맞지만 end 경계가
    `datetime(10000, 1, 1)` 이 되어 `datetime` 이 맨 `ValueError` 를 던진다 —
    ValidationError 가 아니므로 핸들러를 못 타고 **500** 이 됐다(실측).
    """
    m = _PERIOD_RE.match(period) if isinstance(period, str) else None
    if m is None:
        raise ValidationError(f"period must be 'YYYY-MM' (got {period!r})")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValidationError(f"period month must be 01-12 (got {period!r})")
    if not _PERIOD_MIN_YEAR <= year <= _PERIOD_MAX_YEAR:
        raise ValidationError(
            f"period year must be {_PERIOD_MIN_YEAR}-{_PERIOD_MAX_YEAR} (got {period!r})"
        )
    start_kst = datetime(year, month, 1, tzinfo=KST)
    end_kst = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=KST)
    return start_kst.astimezone(timezone.utc), end_kst.astimezone(timezone.utc)


def kst_period_range_filter(column: ColumnElement, period: str) -> ColumnElement:
    """임의의 timestamptz 컬럼에 대한 KST 월 경계 sargable 필터.

    `cost_period_filter` 는 usage_logs 전용이라, productivity_events.created_at /
    git_events.created_at 처럼 다른 테이블의 월 필터에 이걸 쓴다.

    ⚠️ 이 헬퍼가 생긴 이유(정합성 결함): 비용은 KST 월로 묶는데(§59)
    productivity/Git 지표는 `to_char(created_at,'YYYY-MM')`, 즉 **UTC 월**로 묶여
    있었다. 그래서 KST 8/1 00:00~09:00 의 커밋·수락 라인은 7월로 새는데 같은 요청의
    비용은 8월에 잡혀, ROI(=accepted_lines/cost)의 분자와 분모가 **서로 다른 달**을
    보게 됐다. 월초/월말 지표가 실제와 어긋나던 원인.
    """
    start_utc, end_utc = period_to_utc_range(period)
    return and_(column >= start_utc, column < end_utc)


_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def day_range_to_utc(start_day: str, end_day: str) -> tuple[datetime, datetime]:
    """KST 기준 'YYYY-MM-DD' 두 개 → UTC 반개구간 [start, end).

    `end_day` 는 **포함**이다(그 날 하루 전체). 그래서 end 경계는 end_day + 1일의
    KST 00:00 을 UTC 로 옮긴 값이다.

    ⚠️ 문자열을 슬라이싱해서 `int(day[:4])` 로 파싱하지 않는다. 그러면 '20xx-1-1' 같은
    입력이 형식 검사를 통과해 엉뚱한 경계를 만들거나 `int()` 가 맨 ValueError 로 터져
    **500** 이 된다(period_to_utc_range 주석의 같은 교훈). 형식은 정규식으로, 달력
    유효성은 `date.fromisoformat` 으로 확인하고 어긋나면 ValidationError → **400**.
    """
    for label, value in (("start", start_day), ("end", end_day)):
        if not (isinstance(value, str) and _DAY_RE.match(value)):
            raise ValidationError(f"{label} day must be 'YYYY-MM-DD' (got {value!r})")
    try:
        start_date = date.fromisoformat(start_day)
        end_date = date.fromisoformat(end_day)
    except ValueError as exc:  # 2026-06-31 처럼 형식은 맞고 달력엔 없는 날짜
        raise ValidationError(f"invalid calendar date: {exc}") from exc
    if end_date < start_date:
        raise ValidationError(f"end day {end_day} precedes start day {start_day}")
    if not _PERIOD_MIN_YEAR <= start_date.year <= _PERIOD_MAX_YEAR:
        raise ValidationError(
            f"start year must be {_PERIOD_MIN_YEAR}-{_PERIOD_MAX_YEAR} (got {start_day!r})"
        )
    if not _PERIOD_MIN_YEAR <= end_date.year <= _PERIOD_MAX_YEAR:
        raise ValidationError(
            f"end year must be {_PERIOD_MIN_YEAR}-{_PERIOD_MAX_YEAR} (got {end_day!r})"
        )
    start_utc = datetime(
        start_date.year, start_date.month, start_date.day, tzinfo=KST
    ).astimezone(timezone.utc)
    end_exclusive = end_date + timedelta(days=1)
    end_utc = datetime(
        end_exclusive.year, end_exclusive.month, end_exclusive.day, tzinfo=KST
    ).astimezone(timezone.utc)
    return start_utc, end_utc


def kst_day_range_filter(start_day: str, end_day: str) -> ColumnElement:
    """usage_logs 의 KST 일자 구간 sargable 필터 (SUCCESS 조건은 호출부가 붙인다).

    ⚠️ 이 헬퍼가 생긴 이유(성능 결함): 일자 필터가
    `date(timezone('Asia/Seoul', requested_at)) >= date(:start)` 형태였다. 좌변이
    **컬럼에 함수를 씌운 표현식**이라 `requested_at` 인덱스를 전혀 쓰지 못하고
    usage_logs 전체를 훑는다(같은 이유로 월 필터를 고친 것이 cost_period_filter).
    경계 계산을 파라미터 쪽으로 옮기면 컬럼이 그대로 남아 인덱스를 탄다.

    집합은 완전히 동일하다: KST 일자가 [start_day, end_day] 안에 있다는 것은
    requested_at 이 [KST start_day 00:00, KST (end_day+1) 00:00) 안에 있다는 것과
    같다(반개구간이라 경계 중복·누락 없음). 즉 **숫자가 움직이지 않는다.**
    """
    start_utc, end_utc = day_range_to_utc(start_day, end_day)
    return and_(UsageLog.requested_at >= start_utc, UsageLog.requested_at < end_utc)


def cost_day_range_filter(start_day: str, end_day: str, *, success_only: bool = True) -> ColumnElement:
    """`cost_period_filter` 의 일자 범위판 — KST [start_day, end_day] + (기본) SUCCESS 만.

    /admin/analytics 의 `직접 입력` 범위(start_date/end_date)가 월 필터와 **같은 규약**
    (§59: SUCCESS + KST, sargable)으로 집계되게 하는 단일 출처다. 범위 질의에서 한 집계만
    `kst_day_range_filter` 에 SUCCESS 를 빠뜨리면 그 카드만 실패 호출 비용을 더해, 같은
    화면 안에서 요약과 분해의 합계가 어긋난다.
    """
    conds: list[ColumnElement] = [kst_day_range_filter(start_day, end_day)]
    if success_only:
        conds.append(UsageLog.status == UsageStatus.SUCCESS)
    return and_(*conds)


def cost_period_filter(period: str, *, success_only: bool = True) -> ColumnElement:
    """비용 집계 표준 WHERE — KST 월 경계 + (기본) SUCCESS 만.

    기존 `func.to_char(UsageLog.requested_at, "YYYY-MM") == period` (UTC 암묵 +
    status 무필터)을 대체. success_only=False 면 status 필터 생략(전체 호출).

    **sargable 형태로 구현한다.** 과거엔 `kst_month_expr() == period`, 즉
    `to_char(timezone('Asia/Seoul', requested_at),'YYYY-MM') = :period` 였다.
    컬럼이 함수 안에 갇혀 있으면 PostgreSQL 은 B-tree 인덱스를 쓸 수 없고
    (= non-sargable), 매번 전체 행을 스캔해 to_char 를 평가한다. 이 필터의
    소비자가 33곳이라 대시보드 전체가 같은 비용을 물었다.

    실측(598,808행, PostgreSQL 16, 워밍업 2회 후 6회 중위값):

        과거  Parallel Seq Scan   86.7ms   touched buffers 15,373
        현재  Index Scan           7.9ms   touched buffers  1,811   (약 11배)

    ⚠️ **실행시간보다 touched buffers 를 신뢰할 것.** 시간은 page cache 상태에
    좌우된다 — 같은 쿼리의 첫 실행(cold)은 과거형 1,086ms / 현재형 159ms 로 둘 다
    10배 이상 튀었다. cold 와 warm 을 섞어 비교하면 배수가 5배~15배로 요동친다
    (이 독스트링도 한때 5.2배, 15.5배로 잘못 적혀 있었다). 반면 buffer 수는
    캐시·부하와 무관한 구조적 지표라 어느 호스트에서 재보든 같다.

    8개월분 A/B 에서 행수·비용 합계 차이 0, 행단위 대칭차집합도 양방향 0 —
    결과는 동일하다.

    KST 월 경계 의미는 그대로 유지된다. 경계 계산을 SQL 함수에서 파이썬
    파라미터로 옮긴 것뿐이다(period_to_utc_range).
    """
    start_utc, end_utc = period_to_utc_range(period)
    conds: list[ColumnElement] = [
        UsageLog.requested_at >= start_utc,
        UsageLog.requested_at < end_utc,
    ]
    if success_only:
        conds.append(UsageLog.status == UsageStatus.SUCCESS)
    return and_(*conds)


def client_coalesce_expr() -> ColumnElement:
    """usage_logs.client with legacy NULL rows folded into 'other'.

    Use this in GROUP BY so pre-feature rows (client IS NULL) surface as 'other'
    instead of being dropped.
    """
    return func.coalesce(UsageLog.client, "other")


def client_filter(client: str | None) -> ColumnElement | None:
    """Optional WHERE predicate for a dashboard ?client= filter.

    Returns None for 'all'/None/'' (no filtering). For a specific client,
    matches COALESCE(client,'other') so 'other' also catches legacy NULL rows.
    Canonical values: claude-code | cowork | other.
    """
    if not client or client == "all":
        return None
    return client_coalesce_expr() == client
