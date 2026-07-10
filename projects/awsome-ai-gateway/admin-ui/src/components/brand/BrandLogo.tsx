// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * BrandLogo — LLM Gateway 마크의 단일 출처(§60). Sidebar/Header/favicon 이 모두
 * 이 컴포넌트(또는 동일 path)를 쓰도록 해 디자인 드리프트를 막는다.
 *
 * 현재 마크: "고스트 그라데이션"(시안 B1) — 둥근 머리 + 물결 밑단, teal 세로
 * 그라데이션 바디, 흰 눈. 친근한 마스코트(Kiro/Claude 톤) + 브랜드 teal.
 * 보관용 대안: brand/aperture-gradient-tile.svg, brand/aperture-gate.svg.
 *
 * gradient id 는 인스턴스마다 유일해야(한 페이지에 여러 로고가 있어도 충돌 0)
 * 하므로 size 기반 고정 id 대신 호출부에서 idSuffix 로 분리할 수 있게 한다.
 */

interface BrandLogoProps {
  /** 마크 한 변(px). 뱃지가 아니라 글리프 자체 크기. */
  size?: number;
  /** 같은 페이지에 여러 개 렌더 시 gradient id 충돌 방지용 접미사. */
  idSuffix?: string;
  className?: string;
}

export function BrandLogo({ size = 24, idSuffix = 'default', className }: BrandLogoProps) {
  const gid = `gw-ghost-${idSuffix}`;
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 48 48"
      aria-hidden="true"
      className={className}
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#5eead4" />
          <stop offset="1" stopColor="#0d9488" />
        </linearGradient>
      </defs>
      {/* 고스트 바디 — 둥근 머리 + 4-스캘럽 물결 밑단 */}
      <path
        d="M12 22a12 12 0 0 1 24 0v15.5c0 1.2-1.4 1.9-2.4 1.2l-2.5-1.8-2.7 2a1.4 1.4 0 0 1-1.7 0l-2.7-2-2.6 1.9a1.4 1.4 0 0 1-1.7 0l-2.5-1.9-2.4 1.7c-1 .7-2.6 0-2.6-1.2z"
        fill={`url(#${gid})`}
      />
      {/* 눈 — 큰 ink 동공 + 흰 하이라이트(반짝이). 작은 크기서도 또렷+생기(시안 E1). */}
      <circle cx="19.3" cy="22" r="3.4" fill="#0b2b26" />
      <circle cx="28.7" cy="22" r="3.4" fill="#0b2b26" />
      <circle cx="20.5" cy="20.8" r="1.05" fill="#ffffff" />
      <circle cx="29.9" cy="20.8" r="1.05" fill="#ffffff" />
    </svg>
  );
}
