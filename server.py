# 5월 25일 수연
"""
KTAS 중증도 분류 챗봇 - 백엔드 서버 (멀티턴 대화 버전)
실행: python server.py

## 변경 사항 (v2)
- 기존 단답형 → 멀티턴 대화 방식으로 전환
- GPT가 분류 애매 시 최대 MAX_QUESTIONS(=3)회 추가 질문
- 서버는 stateless 유지: 대화 히스토리와 ktas_context를 클라이언트가 캐싱 후 전송

## API 변경
  POST /classify
  Request:
    {
      "messages":      [{"role": "user"|"assistant", "content": "..."}],  # 전체 대화 히스토리
      "ktas_context":  "..."   # 선택 사항: 첫 응답에서 받은 값을 이후 요청마다 재전송
    }
  Response (추가 질문):
    {
      "type":              "question",
      "question":          "숨이 차거나 호흡이 힘드신가요?",
      "question_number":   1,           # 몇 번째 질문인지
      "ktas_context":      "...",       # 첫 응답에서만 포함 – 클라이언트가 저장해야 함
      "matched_complaints": [...]       # 첫 응답에서만 포함
    }
  Response (최종 분류):
    {
      "type":              "classification",
      "level":             2,
      "reason":            "중등도 호흡곤란과 심장성 흉통으로 레벨 2 판정",
      "chief_complaint":   "흉통 (심장성)",
      "matched_complaints": [...]
    }
"""

import json
import re
import os
import requests
from difflib import SequenceMatcher
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv

# ─────────────────────────────────────────
# 초기 설정
# ─────────────────────────────────────────
load_dotenv()
app = Flask(__name__)
CORS(app, origins=[
    "https://suyeonh-ac.github.io",
    "http://localhost:5500",
    "http://localhost:5177",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:5177",
], supports_credentials=True)

# QR 공유 API + 모바일 조회 페이지 (qr_share.py)
from qr_share import bp as qr_share_bp
app.register_blueprint(qr_share_bp)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

CLOVA_CLIENT_ID     = os.getenv("CLOVA_CLIENT_ID")
CLOVA_CLIENT_SECRET = os.getenv("CLOVA_CLIENT_SECRET")

# 국립중앙의료원 응급의료포털 (NEMC) Open API 키
NEMC_API_KEY        = os.environ.get("NEMC_API_KEY", "")

# KTAS 레벨별 최대 추가 질문 횟수
MAX_QUESTIONS_BY_LEVEL = {1:0, 2:0, 3:1, 4:2, 5:2}
MAX_QUESTIONS = 2

# KTAS 데이터 로드
with open("ktas_data.json", encoding="utf-8") as f:
    KTAS_DATA = json.load(f)

print(f"✅ KTAS 데이터 로드 완료: {len(KTAS_DATA)}개 주호소")


# ─────────────────────────────────────────
# RAG: 증상 → 관련 KTAS 항목 검색
# ─────────────────────────────────────────

KEYWORD_MAP = {
    "가슴": ["흉통", "심장성", "비심장성", "심계항진"],
    "흉통": ["흉통", "심장성", "비심장성"],
    "심장": ["흉통", "심장성", "심정지", "심계항진"],
    "심정지": ["심정지"],
    "두통": ["두통"],
    "머리": ["두통", "두부"],
    "복통": ["복통"],
    "배": ["복통", "복부"],
    "아랫배": ["복통", "옆구리"],
    "옆구리": ["옆구리"],
    "호흡": ["호흡", "기침", "상기도"],
    "숨": ["호흡", "기침"],
    "기침": ["기침", "상기도"],
    "어지럼": ["현훈"],
    "어지러": ["현훈"],
    "두근": ["심계항진"],
    "발열": ["열"],
    "고열": ["열"],
    "체온": ["열"],
    "구토": ["구토", "구역"],
    "토": ["구토", "토혈"],
    "설사": ["설사"],
    "혈뇨": ["혈뇨"],
    "혈변": ["혈변", "흑색변"],
    "피": ["출혈", "혈"],
    "골절": ["골절"],
    "외상": ["외상", "손상"],
    "타박": ["외상"],
    "화상": ["화상"],
    "요통": ["요통"],
    "허리": ["요통"],
    "발작": ["발작"],
    "경련": ["발작"],
    "실신": ["실신"],
    "기절": ["실신"],
    "부종": ["부종"],
    "부어": ["부종"],
    "두드러기": ["두드러기", "알레르기"],
    "알레르기": ["알레르기", "두드러기"],
    "가려움": ["두드러기", "피부"],
    "소변": ["소변", "요로"],
    "고혈압": ["고혈압"],
    "혈압": ["고혈압"],
    "뇌졸중": ["뇌졸중", "사지 약화"],
    "마비": ["사지 약화", "뇌졸중"],
    "손발": ["사지 약화", "감각상실"],
    "떨림": ["떨림"],
    "의식": ["의식"],
    "무의식": ["의식"],
    "눈": ["눈"],
    "귀": ["귀", "이통"],
    "코": ["코"],
    "목": ["인후통", "목"],
    "삼킴": ["연하장애"],
    "우울": ["우울증"],
    "불안": ["불안"],
    "자해": ["우울증", "자해"],
    "자살": ["우울증"],
    "임신": ["임신"],
    "생리": ["질"],
    "피부": ["피부"],
    "발진": ["피부", "두드러기"],
    "찰과상": ["열상"],
    "찢어": ["열상"],
    "절단": ["절단"],
    "낙상": ["외상"],
    "교통": ["외상", "다발성"],
    "독": ["중독"],
    "약물": ["중독", "물질오용"],
    "음주": ["알코올"],
    "술": ["알코올"],
}


def search_ktas(symptom_text: str, top_k: int = 5) -> list:
    """사용자 증상에서 관련 KTAS 주호소 항목을 검색합니다."""
    text = symptom_text.lower()
    scores = []

    for entry in KTAS_DATA:
        score = 0.0
        cc = entry["chief_complaint"]
        cc_lower = cc.lower()

        # 1) 키워드 매핑 점수
        for kw, targets in KEYWORD_MAP.items():
            if kw in text:
                for target in targets:
                    if target in cc_lower:
                        score += 3

        # 2) 주호소 단어가 입력에 직접 포함
        cc_words = re.sub(r"[^\w가-힣]", " ", cc).split()
        for word in cc_words:
            if len(word) > 1 and word in symptom_text:
                score += 5

        # 3) 문자열 유사도 (보조)
        sim = SequenceMatcher(None, text, cc_lower).ratio()
        score += sim * 2

        scores.append((score, entry))

    scores.sort(key=lambda x: -x[0])
    return [entry for score, entry in scores[:top_k] if score > 0.1]


def format_ktas_context(entries: list) -> str:
    """검색된 KTAS 항목을 GPT 프롬프트용 텍스트로 변환합니다."""
    if not entries:
        return "관련 항목 없음"

    lines = []
    for entry in entries:
        lines.append(f"\n【{entry['chief_complaint']} (NACRS {entry['nacrs_code']})】")

        if entry["vital_sign_rules"]:
            lines.append("  [활력징후 1차 고려사항]")
            for rule in entry["vital_sign_rules"]:
                lines.append(f"    레벨{rule['level']}: {rule['condition']}")

        if entry["other_first_rules"]:
            lines.append("  [그 밖의 1차 고려사항]")
            for rule in entry["other_first_rules"]:
                lines.append(f"    레벨{rule['level']}: {rule['condition']}")

        if entry["symptom_rules"]:
            lines.append("  [증상별 2차 고려사항]")
            for rule in entry["symptom_rules"]:
                lines.append(f"    레벨{rule['level']}: {rule['condition']}")

    return "\n".join(lines)


# ─────────────────────────────────────────
# GPT 시스템 프롬프트
# ─────────────────────────────────────────

SYSTEM_TEMPLATE = """당신은 KTAS(한국형 응급환자 분류도구, 2021) 전문 분류 시스템입니다.

## KTAS 레벨 정의
- 레벨 1 (소생): 즉각 처치. 심정지, 무의식(GCS 3-8), 쇼크, 중증 호흡곤란
- 레벨 2 (긴급): 15분 이내. 의식변화, 혈역학적 장애, 중등도 호흡곤란, 고위험 기전
- 레벨 3 (응급): 30분 이내. 경증 호흡곤란, 비정상 활력징후(안정), 중등도 통증
- 레벨 4 (준응급): 1시간 이내. 경미한 증상, 경증 통증
- 레벨 5 (비응급): 2시간 이내. 만성 경증, 단순 처방

## 분류 규칙
1. 활력징후 1차 고려사항 → 그 밖의 1차 고려사항 → 증상별 2차 고려사항 순서로 적용
2. 여러 조건 해당 시 가장 높은 중증도(낮은 숫자) 선택

## 행동 지침 (긴급도에 따른 질문 제한)
- 레벨 1~2 의심(심정지, 무의식, 쇼크, 중증 호흡곤란, 패닉 발화): 즉시 분류. 질문 금지.
- 레벨 3 의심: 분류에 결정적인 정보 1가지만 추가 질문 가능 (최대 {max_questions_l3}회)
- 레벨 4~5 의심: 분류 애매 시 최대 {max_questions}회 추가 질문 가능
- 이미 {max_questions}번 질문했으면 → 보수적으로(더 높은 중증도로) 즉시 분류
- 질문은 반드시 KTAS 분류에 직접 영향을 주는 것으로 한정
  (통증 강도 0~10, 호흡곤란 정도, 의식 상태, 발병 시점, 동반 증상 유무 등)
- 이미 답변을 받은 내용은 다시 묻지 말 것

## 관련 KTAS 분류 기준
{ktas_context}

## 병원 추천 우선순위 규칙 (분류와 함께 적용)
1. 환자의 기저질환과 관련된 특수과를 보유한 병원을 최우선으로 추천
2. 환자가 기존에 다니던 병원이 주변에 있으면 동일 병원 계열 우선 고려
3. KTAS 1~2: 거리보다 특수과 보유 + 응급병상 가용 우선
4. KTAS 3: 특수과(0.30) + 거리(0.25) + 대기시간(0.20) + 응급병상(0.20) + 병원등급(0.05) 가중 합산
5. KTAS 4~5: 거리 + 특수과 우선, 응급실보다 야간진료 클리닉도 고려

## 응답 형식 (반드시 JSON만 출력, 다른 텍스트 금지)

추가 질문이 필요한 경우:
{{"type": "question", "question": "<질문 내용을 환자에게 직접 묻는 말투로>"}}

분류 가능한 경우:
{{
  "type": "classification",
  "level": <1~5>,
  "reason": "<분류 근거: 1차 고려사항 적용 결과 → 2차 고려사항 적용 결과 → 최종 레벨 판정 순으로 한국어 2~3문장>",
  "reason_steps": [
    "<1단계: 적용한 활력징후/1차 기준과 결과>",
    "<2단계: 적용한 증상별 2차 기준과 결과>",
    "<3단계: 최종 판정 이유>"
  ],
  "chief_complaint": "<해당 주호소명>",
  "specialty": "<필요 진료과>",
  "hospital_priority": "<특수과 기준 추천 이유 — 30자 이내>",
  "specialty_match": <true/false>
}}
"""


# ─────────────────────────────────────────
# Twin 프로파일 컨텍스트
# ─────────────────────────────────────────

def build_twin_context(profile: dict) -> str:
    """Twin 프로파일 dict를 GPT system prompt용 컨텍스트 텍스트로 변환."""
    if not profile:
        return ""

    lines = ["[환자 Twin 프로파일 — 아래 정보를 분류에 반드시 반영하세요]"]

    if profile.get("age"):
        lines.append(f"- 나이: {profile['age']}세, 성별: {profile.get('gender', '미상')}")

    conditions = profile.get("conditions", [])
    if conditions:
        lines.append(f"- 기저질환: {', '.join(conditions)}")

    medications = profile.get("medications", [])
    if medications:
        lines.append(f"- 복용약: {', '.join(medications)}")

    allergies = profile.get("allergies", [])
    if allergies:
        lines.append(f"- 알레르기: {', '.join(allergies)}")

    regular_hospital = profile.get("regular_hospital")
    if regular_hospital:
        if isinstance(regular_hospital, dict):
            hospital_name = regular_hospital.get("name")
            department = regular_hospital.get("department")
            hospital_text = " / ".join(x for x in [hospital_name, department] if x)
        else:
            hospital_text = str(regular_hospital)
        if hospital_text:
            lines.append(f"- 정기 진료 병원: {hospital_text}")

    hr = profile.get("hr_bpm")
    if hr:
        lines.append(f"- 최근 심박수: {hr}bpm")

    # Twin 프로파일이 있으면 보수적 분류 지침을 함께 명시
    lines.append("- 위 기저질환·복용약을 고려해 합병증 위험이 있으면 한 단계 보수적으로(더 높은 중증도로) 판정하세요")

    return "\n".join(lines)


# ─────────────────────────────────────────
# 멀티턴 분류 함수
# ─────────────────────────────────────────

def count_questions(messages: list) -> int:
    """대화 히스토리에서 이미 질문한 횟수를 셉니다."""
    count = 0
    for m in messages:
        if m.get("role") == "assistant":
            try:
                parsed = json.loads(m["content"])
                if parsed.get("type") == "question":
                    count += 1
            except (json.JSONDecodeError, KeyError):
                pass
    return count


def build_hospital_context(hospital_list) -> str:
    """주변 병원 특수과/병상 정보를 GPT 프롬프트용 텍스트로 변환."""
    if not hospital_list:
        return ""
    lines = ["[주변 응급실 특수과 현황 — 추천 시 반드시 고려]"]
    for i, h in enumerate(hospital_list[:5], 1):
        specs = ', '.join(h.get('specialties', []) or []) or '정보 없음'
        er = h.get('er_available', h.get('availBeds', '?'))
        lines.append(f"{i}. {h.get('name', '병원')} — 특수과: {specs} / 응급병상: {er}")
    return "\n".join(lines)


def classify_with_gpt(messages: list, ktas_context: str, question_count: int,
                      twin_context: str = "", hospital_list=None) -> dict:
    """
    멀티턴 대화 히스토리 + KTAS 컨텍스트 (+선택적 Twin/주변 병원 컨텍스트)를
    바탕으로 GPT가 분류 또는 추가 질문을 반환합니다.
    """
    # KTAS 레벨별 질문 횟수 제한 계산
    max_q_l3 = MAX_QUESTIONS_BY_LEVEL.get(3, 1)
    max_q    = MAX_QUESTIONS_BY_LEVEL.get(5, 2)

    system_prompt = SYSTEM_TEMPLATE.format(
        max_questions=max_q,
        max_questions_l3=max_q_l3,
        ktas_context=ktas_context,
    )

    # 주변 병원 정보가 있으면 prepend
    hospital_context = build_hospital_context(hospital_list)
    if hospital_context:
        system_prompt = hospital_context + "\n\n" + system_prompt

    # Twin 프로파일이 있으면 그 앞에 prepend (최상단)
    if twin_context:
        system_prompt = twin_context + "\n\n" + system_prompt

    # 최대 질문 횟수에 도달했으면 강제 분류 지시
    if question_count >= MAX_QUESTIONS:
        system_prompt += (
            f"\n\n[필수] 이미 {MAX_QUESTIONS}번 질문했습니다. "
            "추가 질문 없이 지금 바로 분류해야 합니다."
        )

    gpt_messages = [{"role": "system", "content": system_prompt}] + messages

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},  # JSON 모드 강제
        messages=gpt_messages,
    )

    raw = response.choices[0].message.content.strip()
    return json.loads(raw)


# ─────────────────────────────────────────
# Flask 라우트
# ─────────────────────────────────────────

@app.route("/stt", methods=["POST"])
def stt():
    """
    OpenAI Whisper STT API 프록시.
    프론트엔드에서 직접 호출하면 CORS 차단되므로 백엔드가 대신 호출.

    Request : multipart/form-data  { audio: <audio blob> }
    Response: { "text": "인식된 텍스트" }
    """
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "오디오 파일이 없습니다."}), 400

    try:
        # OpenAI Whisper API 호출
        # 파일명에 확장자 포함 필요 (Whisper가 포맷 감지에 사용)
        filename = audio_file.filename or "audio.webm"
        if "." not in filename:
            filename = "audio.webm"

        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_file.read(), audio_file.content_type or "audio/webm"),
            language="ko",          # 한국어 고정 → 정확도 향상
            response_format="text", # 텍스트만 반환
        )

        # response_format="text"이면 문자열 직접 반환
        recognized = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
        print(f"✅ Whisper STT 인식: {recognized!r}")
        return jsonify({"text": recognized})

    except Exception as e:
        print(f"❌ Whisper STT 오류: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/classify", methods=["POST"])
def classify():
    """
    멀티턴 KTAS 분류 API.

    첫 번째 요청 예시:
      { "messages": [{"role": "user", "content": "가슴이 많이 아파요"}] }

    두 번째 이후 요청 예시 (클라이언트가 ktas_context 캐싱 후 재전송):
      {
        "messages": [
          {"role": "user",      "content": "가슴이 많이 아파요"},
          {"role": "assistant", "content": "{\"type\":\"question\",\"question\":\"숨이 차신가요?\"}"},
          {"role": "user",      "content": "숨은 안 차요"}
        ],
        "ktas_context": "..."
      }
    """
    body = request.get_json(force=True, silent=True) or {}
    messages: list = body.get("messages", [])
    ktas_context: str = body.get("ktas_context", "").strip()
    twin_profile: dict = body.get("twin_profile", None)  # Twin 프로파일 (선택)
    hospital_list: list = body.get("hospital_list", None)  # 주변 병원 (선택)

    # ── 입력 검증 ──────────────────────────
    if not messages:
        return jsonify({"error": "messages 배열이 비어 있습니다."}), 400

    # 첫 번째 사용자 메시지 = 초기 증상
    first_user = next(
        (m["content"] for m in messages if m.get("role") == "user"), ""
    ).strip()
    if not first_user:
        return jsonify({"error": "증상을 입력해주세요."}), 400
    if len(first_user) > 500:
        return jsonify({"error": "증상이 너무 깁니다 (최대 500자)."}), 400

    # ── RAG: 첫 호출일 때만 실행 ────────────
    matched_complaints: list = []
    is_first_call = not ktas_context
    if is_first_call:
        relevant_entries = search_ktas(first_user, top_k=5)
        ktas_context = format_ktas_context(relevant_entries)
        matched_complaints = [e["chief_complaint"] for e in relevant_entries]

    # ── 현재까지 질문 횟수 카운트 ────────────
    question_count = count_questions(messages)

    # ── Twin 컨텍스트 빌드 (없으면 빈 문자열) ─
    twin_context = build_twin_context(twin_profile)

    # ── GPT 호출 ────────────────────────────
    try:
        result = classify_with_gpt(messages, ktas_context, question_count,
                                   twin_context, hospital_list)
    except Exception as e:
        print(f"❌ GPT 오류: {e}")
        return jsonify({"error": str(e)}), 500

    # ── 응답 메타데이터 추가 ─────────────────
    result_type = result.get("type")

    if result_type == "question":
        result["question_number"] = question_count + 1

    # 첫 호출에서만 ktas_context와 matched_complaints를 내려보냄
    # 클라이언트는 이 값을 저장해 두고 다음 요청에 재전송해야 함
    if is_first_call:
        result["ktas_context"] = ktas_context
        result["matched_complaints"] = matched_complaints

    if result_type == "classification" and matched_complaints:
        result["matched_complaints"] = matched_complaints

    # ── Twin/병원 추천 메타 ─────────────────
    if result_type == "classification":
        result["twin_used"] = bool(twin_profile)
        result.setdefault("specialty", "응급의학과")
        result.setdefault("hospital_priority", "")
        result.setdefault("specialty_match", False)
        result.setdefault("reason_steps", [])  # 단계별 분류 근거

    return jsonify(result)


# ─────────────────────────────────────────
# ─────────────────────────────────────────
# NEMC 응급의료포털 API 통합 (3개 엔드포인트 조합)
# ─────────────────────────────────────────
# 1. getEgytLcinfoInqire               : 위경도 반경 내 응급의료기관 위치 조회
# 2. getEmrrmRltmUsefulSckbdInfoInqire : 실시간 가용병상 조회
# 3. getSrsillDissAceptncPosblInfoInqire : 중증질환 수용가능 정보 조회
# ─────────────────────────────────────────

NEMC_BASE = "https://apis.data.go.kr/B552657/ErmctInfoInqireService"

# 특수과 가용여부 필드 (병상수 > 0 이면 가능)
NEMC_SPECIALTY_FIELDS = {
    'hvs01': '신경과',    'hvs02': '흉부외과',   'hvs03': '신경외과',
    'hvs04': '심장내과',  'hvs05': '소화기내과', 'hvs06': '산부인과',
    'hvs07': '비뇨기과',  'hvs08': '내분비내과', 'hvs09': '응급의학과',
    'hvs10': '소아과',    'hvs11': '정형외과',   'hvs12': '안과',
    'hvs13': '화상',      'hvs14': '정신건강의학과',
}

# 중증질환 수용가능 필드 (Y/N)
NEMC_SERIOUS_FIELDS = {
    'hv27': '뇌졸중',     'hv28': '심근경색',  'hv29': '외상',
    'hv30': '뇌출혈수술', 'hv31': '심장수술',  'hv32': '복부수술',
    'hv33': '정형외과수술','hv35': '화상',      'hv37': '당뇨고혈당',
    'hv38': '뇌경색',     'hv39': '폐렴',
}


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_items(data: dict) -> list:
    items = data.get('response', {}).get('body', {}).get('items', {})
    if not items:
        return []
    item = items.get('item', [])
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else []


def _nemc_get(endpoint: str, params: dict, timeout: int = 7) -> list:
    try:
        url = f"{NEMC_BASE}/{endpoint}"
        params['serviceKey'] = NEMC_API_KEY
        params['_type'] = 'json'
        params['pageNo'] = 1
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return _parse_items(resp.json())
    except Exception as e:
        print(f"[NEMC:{endpoint}] {type(e).__name__}: {e}")
        return []


def fetch_nearby_hospitals(lat: float, lng: float) -> list:
    """
    1단계: getEgytListInfoInqire로 위치 정보 조회 (좌표 포함)
    2단계: getEmrrmRltmUsefulSckbdInfoInqire로 실시간 병상 조회
    3단계: hpid로 두 결과 합치기
    """
    import math

    # 1단계: 위치 정보 조회 (전국 응급의료기관 목록 - 좌표 포함)
    loc_items = _nemc_get('getEgytListInfoInqire', {
        'numOfRows': 500,
    })

    if not loc_items:
        print("[NEMC] 위치 목록 조회 실패")
        return []

    print(f"[NEMC] 위치 목록: {len(loc_items)}개")
    if loc_items:
        first = loc_items[0]
        print(f"[NEMC DEBUG] 위치 항목 키: {list(first.keys())[:15]}")
        print(f"[NEMC DEBUG] 좌표 샘플: wgs84Lat={first.get('wgs84Lat')}, wgs84Lon={first.get('wgs84Lon')}")

    # 30km 이내 병원만 필터링
    nearby = []
    for item in loc_items:
        hlat = _safe_float(item.get('wgs84Lat') or item.get('wgs84lat', 0))
        hlng = _safe_float(item.get('wgs84Lon') or item.get('wgs84lon', 0))
        if not (hlat and hlng):
            continue
        dlat = math.radians(hlat - lat)
        dlng = math.radians(hlng - lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(hlat)) * math.sin(dlng/2)**2
        dist_km = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        if dist_km > 30:
            continue
        nearby.append({
            'hpid':     item.get('hpid', ''),
            'name':     item.get('dutyName', ''),
            'address':  item.get('dutyAddr', ''),
            'phone':    item.get('dutyTel3', '') or item.get('dutyTel1', ''),
            'lat':      hlat,
            'lng':      hlng,
            'er_type':  item.get('dutyDivNam', ''),
            'dist_km':  round(dist_km, 2),
        })

    print(f"[NEMC] 30km 이내 응급실: {len(nearby)}개")
    if not nearby:
        return []

    # 2단계: 실시간 병상 조회
    bed_items = _nemc_get('getEmrrmRltmUsefulSckbdInfoInqire', {
        'STAGE1': '',
        'STAGE2': '',
        'numOfRows': 500,
    })
    bed_map = {}
    for item in bed_items:
        hpid = item.get('hpid', '')
        if not hpid:
            continue
        specialties = [
            name for field, name in NEMC_SPECIALTY_FIELDS.items()
            if _safe_int(item.get(field, 0)) > 0
        ]
        serious = [
            name for field, name in NEMC_SERIOUS_FIELDS.items()
            if str(item.get(field, '')).upper() in ('Y', '1')
        ]
        bed_map[hpid] = {
            'availBeds':      _safe_int(item.get('hvec', 0)),
            'surgeryBeds':    _safe_int(item.get('hvoc', 0)),
            'icuBeds':        _safe_int(item.get('hvncc', 0)),
            'specialties':    specialties,
            'serious_accept': serious,
            'updated_at':     item.get('hvidate', ''),
        }

    # 3단계: hpid로 합치기
    result = []
    for h in nearby:
        hpid = h['hpid']
        bed  = bed_map.get(hpid, {})
        result.append({
            **h,
            'availBeds':      bed.get('availBeds', 0),
            'er_available':   bed.get('availBeds', 0),
            'surgeryBeds':    bed.get('surgeryBeds', 0),
            'icuBeds':        bed.get('icuBeds', 0),
            'specialties':    bed.get('specialties', []),
            'serious_accept': bed.get('serious_accept', []),
            'realtime':       hpid in bed_map,
            'updated_at':     bed.get('updated_at', ''),
        })

    result.sort(key=lambda x: x['dist_km'])
    realtime_cnt = sum(1 for h in result if h['realtime'])
    print(f"[NEMC] 통합 완료: {len(result)}개, 실시간 매칭 {realtime_cnt}개")
    return result


def fetch_realtime_beds() -> dict:
    items = _nemc_get('getEmrrmRltmUsefulSckbdInfoInqire', {
        'STAGE1': '',
        'STAGE2': '',
        'numOfRows': 200,
    })
    bed_map = {}
    for item in items:
        hpid = item.get('hpid', '')
        if not hpid:
            continue
        specialties = [
            name for field, name in NEMC_SPECIALTY_FIELDS.items()
            if _safe_int(item.get(field, 0)) > 0
        ]
        serious = [
            name for field, name in NEMC_SERIOUS_FIELDS.items()
            if str(item.get(field, '')).upper() in ('Y', '1')
        ]
        bed_map[hpid] = {
            'availBeds':      _safe_int(item.get('hvec', 0)),
            'surgeryBeds':    _safe_int(item.get('hvoc', 0)),
            'icuBeds':        _safe_int(item.get('hvcc', 0)),
            'specialties':    specialties,
            'serious_accept': serious,
            'updated_at':     item.get('hvidate', ''),
        }
    return bed_map


def fetch_emergency_hospitals(lat: float, lng: float, radius_km: int = 10) -> list:
    if not NEMC_API_KEY:
        print("[NEMC] API key 미설정 → 카카오 fallback 사용")
        return []
    return fetch_nearby_hospitals(lat, lng)


@app.route("/hospitals", methods=["GET"])
def hospitals():
    try:
        lat = float(request.args.get('lat', '0'))
        lng = float(request.args.get('lng', '0'))
    except ValueError:
        return jsonify({"error": "lat/lng must be numeric"}), 400

    if lat == 0 or lng == 0:
        return jsonify({"error": "lat, lng are required"}), 400

    items = fetch_emergency_hospitals(lat, lng)
    realtime_cnt = sum(1 for h in items if h.get('realtime'))

    return jsonify({
        "hospitals":      items,
        "source":         "nemc" if items else "empty",
        "count":          len(items),
        "realtime_count": realtime_cnt,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":       "ok",
        "ktas_entries": len(KTAS_DATA),
        "nemc_ready":   bool(NEMC_API_KEY),
        "nemc_apis": [
            "getEgytLcinfoInqire",
            "getEmrrmRltmUsefulSckbdInfoInqire",
            "getSrsillDissAceptncPosblInfoInqire",
        ]
    })



if __name__ == "__main__":
    print("🚀 서버 시작: http://localhost:5000")
    print("   index.html을 브라우저로 여세요")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
