// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 추가/편집 폼 ↔ zod 스키마 ↔ 백엔드 요청 스키마의 정합성 회귀 테스트.
 *
 * 배경(실제 고객 장애): ModelCreateSchema 가 `max_tokens`/`context_window` 를 required 로
 * 요구했지만 CreateModelDialog 의 폼에는 두 필드의 입력란이 없고 payload 에도 넣지 않았다.
 * 백엔드 ModelCreateRequest 와 model.model_aliases 테이블에도 대응 필드가 없다.
 * 결과: 모든 모델 추가·편집이 항상 safeParse 실패 → 화면에 "Validation failed" 만 표시.
 * 게다가 두 키에는 대응 <input> 이 없어 fieldErrors 가 렌더되지 않아 원인이 보이지 않았다.
 *
 * 이 테스트는 "폼이 실제로 만드는 payload" 를 기준으로 검증하므로, 폼에 없는 필드를
 * 스키마에 다시 추가하면 즉시 실패한다.
 */

import { describe, it, expect } from 'vitest';
import { ModelCreateSchema } from '@/types/api';

/**
 * CreateModelDialog.handleSubmit 의 payload 구성을 그대로 재현한다.
 * (폼 FormState 는 전부 string 이고 숫자는 parseFloat 로 변환된다.)
 */
function buildPayloadLikeDialog(form: {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string;
  input_price_per_1k: string;
  output_price_per_1k: string;
  cache_creation_5m_price_per_1k: string;
  cache_creation_1h_price_per_1k: string;
  cache_read_price_per_1k: string;
  description: string;
  display_name: string;
}) {
  return {
    alias: form.alias,
    provider: form.provider,
    model_id: form.model_id,
    endpoint_url: form.endpoint_url,
    input_price_per_1k: parseFloat(form.input_price_per_1k),
    output_price_per_1k: parseFloat(form.output_price_per_1k),
    cache_creation_5m_price_per_1k: parseFloat(form.cache_creation_5m_price_per_1k || '0'),
    cache_creation_1h_price_per_1k: parseFloat(form.cache_creation_1h_price_per_1k || '0'),
    cache_read_price_per_1k: parseFloat(form.cache_read_price_per_1k || '0'),
    description: form.description || undefined,
    display_name: form.display_name || undefined,
  };
}

/** 고객이 실제로 입력했던 GPT-5.6 Sol 등록 값(스크린샷 기준). */
const GPT56_SOL_FORM = {
  alias: 'codex-gpt-5.6-sol',
  provider: 'BEDROCK_MANTLE_OPENAI',
  model_id: 'openai.gpt-5.6-sol',
  endpoint_url: 'https://bedrock-mantle.us-east-2.api.aws/openai',
  input_price_per_1k: '0.011000',
  output_price_per_1k: '0.049500',
  cache_creation_5m_price_per_1k: '0.013750',
  cache_creation_1h_price_per_1k: '0',
  cache_read_price_per_1k: '0.001100',
  description: '',
  display_name: 'Codex · GPT-5.6 Sol',
};

describe('ModelCreateSchema — 폼 payload 정합성', () => {
  it('고객이 입력한 GPT-5.6 Sol 등록 payload 를 통과시킨다 (회귀: Validation failed)', () => {
    const parsed = ModelCreateSchema.safeParse(buildPayloadLikeDialog(GPT56_SOL_FORM));

    // 실패 시 어떤 필드가 막았는지 바로 보이도록 이슈를 노출한다.
    const issues = parsed.success
      ? []
      : parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
    expect(issues).toEqual([]);
    expect(parsed.success).toBe(true);
  });

  it('세 자리 Mantle/Bedrock/OPENMODEL provider 모두 등록 가능하다', () => {
    for (const provider of ['BEDROCK', 'OPENMODEL', 'BEDROCK_MANTLE', 'BEDROCK_MANTLE_OPENAI']) {
      const parsed = ModelCreateSchema.safeParse(
        buildPayloadLikeDialog({ ...GPT56_SOL_FORM, provider })
      );
      expect(parsed.success, `provider=${provider}`).toBe(true);
    }
  });

  it('스키마의 required 키가 폼이 보내는 키 집합을 벗어나지 않는다 (드리프트 방지)', () => {
    // 폼이 실제로 만들 수 있는 키 목록. 여기에 없는 키를 스키마가 required 로 요구하면
    // 사용자는 절대 값을 채울 수 없다.
    const KEYS_THE_FORM_CAN_SEND = new Set([
      'alias',
      'provider',
      'model_id',
      'endpoint_url',
      'input_price_per_1k',
      'output_price_per_1k',
      'cache_creation_5m_price_per_1k',
      'cache_creation_1h_price_per_1k',
      'cache_read_price_per_1k',
      'description',
      'display_name',
    ]);

    // 빈 객체를 파싱해 "값이 없으면 실패하는 키"(=사실상 required)를 뽑아낸다.
    // optional / .default() 가 붙은 키는 여기에 나타나지 않는다.
    const empty = ModelCreateSchema.safeParse({});
    const requiredKeys = empty.success
      ? []
      : [...new Set(empty.error.issues.map((i) => i.path.join('.')))];

    const unreachable = requiredKeys.filter((k) => !KEYS_THE_FORM_CAN_SEND.has(k));
    expect(unreachable).toEqual([]);
  });

  it('설명·표시이름은 비워도 통과한다 (선택 필드)', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, description: '', display_name: '' })
    );
    expect(parsed.success).toBe(true);
  });

  it('필수 필드가 비면 해당 필드에 에러가 붙는다 (안내가 화면에 보여야 함)', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, alias: '', model_id: '' })
    );
    expect(parsed.success).toBe(false);
    if (!parsed.success) {
      const keys = parsed.error.issues.map((i) => i.path.join('.'));
      expect(keys).toContain('alias');
      expect(keys).toContain('model_id');
    }
  });

  it('음수 단가는 거부한다', () => {
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, input_price_per_1k: '-0.01' })
    );
    expect(parsed.success).toBe(false);
  });

  it('단가 미입력(NaN)은 거부한다 — 조용히 0 으로 청구되면 안 된다', () => {
    // 사용자가 입력란을 비우면 parseFloat('') === NaN 이 된다.
    const parsed = ModelCreateSchema.safeParse(
      buildPayloadLikeDialog({ ...GPT56_SOL_FORM, input_price_per_1k: '' })
    );
    expect(parsed.success).toBe(false);
  });
});
