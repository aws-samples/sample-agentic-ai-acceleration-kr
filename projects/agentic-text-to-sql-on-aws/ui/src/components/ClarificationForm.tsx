// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useMemo, useState } from 'react';

/**
 * ============================================================================
 * ClarificationForm — clarification(재요청) 인터랙티브 폼 (M2)
 * ============================================================================
 * orchestrator 가 정보 부족 시 방출하는 AG-UI CUSTOM 이벤트(name="clarification_request")의
 * value 스키마를 렌더한다. 사용자가 폼을 제출하면 동일 threadId 로 재실행되며
 * forwardedProps.clarificationResponse = { interruptId, values } 로 응답을 전달한다.
 *
 * 이 컴포넌트는 순수 프레젠테이션 — 이벤트 수신·재실행 트리거는 T2SChat 의 host 로직이 담당.
 * value 스키마(확정 계약, 변경 금지):
 *   { interruptId, interruptName, question,
 *     fields: [{ name, label, type: "select"|"date_range"|"text", options?: [] }] }
 * date_range 값은 { from: "YYYY-MM-DD", to: "YYYY-MM-DD" }.
 */

export type ClarificationFieldType = 'select' | 'date_range' | 'text';

export interface ClarificationField {
  name: string;
  label: string;
  type: ClarificationFieldType;
  options?: string[];
}

export interface ClarificationRequest {
  /** 재실행 시 그대로 돌려보낼 인터럽트 식별자. */
  interruptId: string;
  interruptName?: string;
  question: string;
  fields: ClarificationField[];
}

/** 제출되는 응답 값. date_range 는 { from, to } 객체, 그 외는 문자열. */
export type ClarificationValue = string | { from: string; to: string };
export type ClarificationValues = Record<string, ClarificationValue>;

interface ClarificationFormProps {
  request: ClarificationRequest;
  /** 제출: 유효 입력만 담긴 values (건너뛰기는 빈 객체). */
  onSubmit: (_values: ClarificationValues) => void;
  /** 건너뛰기: 빈 values 로 재실행(orchestrator 가 최선 추정으로 진행). */
  onSkip: () => void;
  /** 재실행 트리거 후 폼 비활성화. */
  disabled?: boolean;
}

/**
 * 필드 상태를 응답 values 로 정규화한다.
 * - text/select: 공백 제거 후 비어있지 않은 값만 포함.
 * - date_range: from·to 중 하나라도 있으면 { from, to } 포함(빈 쪽은 "").
 */
function toValues(
  fields: ClarificationField[],
  text: Record<string, string>,
  range: Record<string, { from: string; to: string }>
): ClarificationValues {
  const values: ClarificationValues = {};
  for (const field of fields) {
    if (field.type === 'date_range') {
      const r = range[field.name] ?? { from: '', to: '' };
      if (r.from || r.to) values[field.name] = { from: r.from, to: r.to };
    } else {
      const v = (text[field.name] ?? '').trim();
      if (v) values[field.name] = v;
    }
  }
  return values;
}

export function ClarificationForm({ request, onSubmit, onSkip, disabled }: ClarificationFormProps) {
  const { question, fields } = request;

  // select/text 는 문자열, date_range 는 { from, to } 로 분리 관리.
  const [text, setText] = useState<Record<string, string>>({});
  const [range, setRange] = useState<Record<string, { from: string; to: string }>>({});

  const values = useMemo(() => toValues(fields, text, range), [fields, text, range]);
  const hasInput = Object.keys(values).length > 0;

  const setTextField = (name: string, value: string) =>
    setText((prev) => ({ ...prev, [name]: value }));
  const setRangeField = (name: string, key: 'from' | 'to', value: string) =>
    setRange((prev) => {
      const current = prev[name] ?? { from: '', to: '' };
      return { ...prev, [name]: { ...current, [key]: value } };
    });

  return (
    <form
      className="t2s-clarify"
      role="form"
      aria-label="추가 정보 요청"
      onSubmit={(e) => {
        e.preventDefault();
        if (disabled) return;
        onSubmit(values);
      }}
    >
      <div className="t2s-clarify-question">
        <span className="t2s-clarify-icon" aria-hidden>
          ?
        </span>
        <span>{question}</span>
      </div>

      <div className="t2s-clarify-fields">
        {fields.map((field) => (
          <div key={field.name} className="t2s-clarify-field">
            <label className="t2s-clarify-label" htmlFor={`t2s-clarify-${field.name}`}>
              {field.label}
            </label>

            {field.type === 'select' ? (
              <select
                id={`t2s-clarify-${field.name}`}
                className="t2s-clarify-input"
                value={text[field.name] ?? ''}
                disabled={disabled}
                onChange={(e) => setTextField(field.name, e.target.value)}
              >
                <option value="">선택하세요</option>
                {(field.options ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : field.type === 'date_range' ? (
              <div className="t2s-clarify-range">
                <input
                  id={`t2s-clarify-${field.name}`}
                  type="date"
                  className="t2s-clarify-input"
                  aria-label={`${field.label} 시작일`}
                  value={range[field.name]?.from ?? ''}
                  disabled={disabled}
                  onChange={(e) => setRangeField(field.name, 'from', e.target.value)}
                />
                <span className="t2s-clarify-range-sep" aria-hidden>
                  ~
                </span>
                <input
                  type="date"
                  className="t2s-clarify-input"
                  aria-label={`${field.label} 종료일`}
                  value={range[field.name]?.to ?? ''}
                  disabled={disabled}
                  onChange={(e) => setRangeField(field.name, 'to', e.target.value)}
                />
              </div>
            ) : (
              <input
                id={`t2s-clarify-${field.name}`}
                type="text"
                className="t2s-clarify-input"
                value={text[field.name] ?? ''}
                disabled={disabled}
                onChange={(e) => setTextField(field.name, e.target.value)}
              />
            )}
          </div>
        ))}
      </div>

      <div className="t2s-clarify-actions">
        <button
          type="submit"
          className="t2s-clarify-btn t2s-clarify-btn-primary"
          disabled={disabled || !hasInput}
        >
          제출
        </button>
        <button
          type="button"
          className="t2s-clarify-btn"
          disabled={disabled}
          onClick={() => onSkip()}
        >
          건너뛰기
        </button>
      </div>
    </form>
  );
}
