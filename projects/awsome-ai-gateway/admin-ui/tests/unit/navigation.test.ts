// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 내비게이션 도달성 — "페이지는 있는데 갈 수가 없다" 를 막는다.
 *
 * 회귀 배경(엔드포인트 스윕):
 *   * 한때 page.tsx·PAGE_PERMISSIONS·번역키가 모두 있는데 Sidebar 의 NAV_ITEMS 에만 없어서,
 *     URL 을 직접 치지 않으면 도달할 수 없는 화면이 있었다(사이드바가 유일한
 *     내비게이션이다).
 *   * 반대 함정: 메뉴에 넣었는데 PAGE_PERMISSIONS 가 그 역할을 막으면, 눌러서 /403 으로
 *     튕기는 메뉴가 된다. 두 표가 어긋나지 않는지 소스에서 직접 확인한다.
 *
 * Sidebar 는 'use client' + JSX 아이콘이라 런타임 import 대신 소스를 읽어 대조한다
 * (아이콘/스타일 변경에 깨지지 않게 href·allowedRoles 만 본다).
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PAGE_PERMISSIONS } from '@/lib/permissions';
import { checkPagePermission } from '@/lib/auth';
import type { UserRole } from '@/types/enums';

const SRC = join(process.cwd(), 'src');
const SIDEBAR = readFileSync(join(SRC, 'components/layout/Sidebar.tsx'), 'utf8');

/**
 * 주석을 제거한다 — 주석 안의 예시가 코드로 오독되지 않게.
 *
 * ⚠️ 정규식 두 줄로는 안 된다. 처음엔 블록주석을 `\/\*[\s\S]*?\*\/` 로 걷어냈는데,
 *    app/cli/page.tsx 의 주석에 적힌 글롭 `app/api/**` 의 `/**` 를 블록주석 시작으로 읽고
 *    한참 뒤의 JSX 주석 닫는 표시까지 **코드 20줄을 통째로 삭제**했다. 그러면
 *    "금지된 경로를 쓰지 않는다" 는 단정이 공허하게 통과한다 — 영역 자체가 사라졌으니까.
 *    (대조군 `toContain('/api/cli-download/')` 이 이 사고를 잡아냈다.)
 *    그래서 문자열·템플릿 리터럴을 인식하는 최소 스캐너로 처리한다.
 *
 * 한계: 정규식 리터럴은 문자열로 보지 않는다(대상 파일들에는 없다). 줄 번호가 어긋나지
 * 않도록 개행은 보존한다.
 */
function stripComments(src: string): string {
  let out = '';
  let state: 'code' | 'line' | 'block' | "'" | '"' | '`' = 'code';
  let i = 0;

  while (i < src.length) {
    const c = src[i];
    const d = src[i + 1];

    if (state === 'code') {
      if (c === '/' && d === '/') { state = 'line'; i += 2; continue; }
      if (c === '/' && d === '*') { state = 'block'; i += 2; continue; }
      if (c === "'" || c === '"' || c === '`') state = c;
      out += c; i += 1; continue;
    }

    if (state === 'line') {
      if (c === '\n') { state = 'code'; out += c; }
      i += 1; continue;
    }

    if (state === 'block') {
      if (c === '*' && d === '/') { state = 'code'; i += 2; continue; }
      if (c === '\n') out += c; // 줄 번호 유지
      i += 1; continue;
    }

    // 문자열/템플릿 리터럴 내부 — 여기서는 `//` 도 `/*` 도 주석이 아니다.
    if (c === '\\') { out += c + (d ?? ''); i += 2; continue; }
    if (c === state) state = 'code';
    out += c; i += 1;
  }

  return out;
}

/**
 * NAV_ITEMS 각 항목의 (key, href, allowedRoles) 를 소스에서 뽑는다.
 *
 * ⚠️ 반드시 주석을 먼저 걷어낸다. 이 표의 항목에는 "왜 이 역할인지" 를 적은 주석이 붙어 있고,
 *    그 주석이 `allowedRoles: [...]` 형태의 예시를 담으면 정규식이 **주석 값**을 읽어
 *    실제 배포되는 역할과 다른 값으로 조용히 통과한다(가드가 초록인데 화면은 틀린 상태).
 *    key/href 의 순서가 바뀌어도 잡히도록 두 순서를 모두 시도한다.
 */
function parseNavItems(): { key: string; href: string; roles: string[] }[] {
  const src = stripComments(SIDEBAR);
  const block = src.slice(src.indexOf('const NAV_ITEMS'), src.indexOf('export function Sidebar'));
  const items: { key: string; href: string; roles: string[] }[] = [];

  // 객체 하나씩 잘라서 본다 — 항목 경계를 넘어 매칭되는 일을 막는다.
  for (const chunk of block.split(/\}\s*,/)) {
    const key = chunk.match(/key:\s*'([^']+)'/);
    const href = chunk.match(/href:\s*'([^']+)'/);
    const roles = chunk.match(/allowedRoles:\s*\[([^\]]*)\]/);
    if (!key || !href || !roles) continue;
    items.push({
      key: key[1],
      href: href[1],
      roles: [...roles[1].matchAll(/UserRoleConst\.(\w+)/g)].map((r) => r[1]),
    });
  }
  return items;
}

const NAV = parseNavItems();

/**
 * 이 파일의 NAV 파싱 가드가 stripComments 위에 서 있다.
 * 스트리퍼가 코드를 지워 버리면 가드가 조용히 공허해지므로 스트리퍼 자체를 검사한다.
 */
describe('stripComments (가드의 토대)', () => {
  it('주석 안의 값은 지우고 코드는 남긴다', () => {
    const out = stripComments(
      ["// allowedRoles: [UserRoleConst.ADMIN]", "const x = 'keep';", '/* block */ const y = 1;'].join('\n'),
    );
    expect(out).not.toContain('UserRoleConst.ADMIN');
    expect(out).toContain("const x = 'keep';");
    expect(out).toContain('const y = 1;');
  });

  it('주석 속 `/**` 글롭이 뒤따르는 코드를 삼키지 않는다 (실제 회귀)', () => {
    // app/cli/page.tsx 가 정확히 이 모양이었다: 주석의 app/api/** 가 블록주석을 열고
    // 아래 JSX 주석의 닫는 표시에서 끝나면서 그 사이 코드가 전부 사라졌다.
    const out = stripComments(
      [
        '// 규약은 `app/api/**` 이다',
        "const keepMe = '/api/cli-download/x';",
        '{/* JSX 주석 */}',
        'const alsoKeepMe = 2;',
      ].join('\n'),
    );
    expect(out).toContain("'/api/cli-download/x'");
    expect(out).toContain('const alsoKeepMe = 2;');
    expect(out).not.toContain('JSX 주석');
  });

  it('문자열 안의 // 는 주석이 아니다', () => {
    const out = stripComments("const u = 'http://example.com/a';");
    expect(out).toContain("'http://example.com/a'");
  });

  it('개행을 보존한다(줄 번호가 어긋나지 않게)', () => {
    const src = 'a\n// c\nb\n';
    expect(stripComments(src).split('\n').length).toBe(src.split('\n').length);
  });
});

describe('Sidebar NAV_ITEMS', () => {
  it('parses (sanity — 정규식이 조용히 0개를 반환하면 이 파일 전체가 공허해진다)', () => {
    expect(NAV.length).toBeGreaterThanOrEqual(9);
    for (const item of NAV) {
      expect(item.roles.length, `${item.key} has no allowedRoles`).toBeGreaterThan(0);
    }
  });

  it('has an entry for every page in PAGE_PERMISSIONS', () => {
    const linked = new Set(NAV.map((i) => i.href));
    // /403 은 에러 페이지라 메뉴에 넣지 않는다. 그 외 권한표의 모든 경로는 도달 가능해야 한다.
    const expected = Object.keys(PAGE_PERMISSIONS).filter((p) => p !== '/403');
    const missing = expected.filter((p) => !linked.has(p));
    expect(missing, `사이드바에서 도달할 수 없는 페이지: ${missing.join(', ')}`).toEqual([]);
  });

  it('never shows a menu item that the role cannot open (→ /403)', () => {
    for (const item of NAV) {
      for (const role of item.roles) {
        expect(
          checkPagePermission(item.href, role as UserRole),
          `${item.key}(${item.href}) 메뉴가 ${role} 에게 보이는데 PAGE_PERMISSIONS 는 막는다`,
        ).toBe(true);
      }
    }
  });

  it('shows it to every role the permission table allows (양방향 일치)', () => {
    // 한쪽 방향만 보면 드리프트의 절반을 놓친다. 실제로 chat 메뉴가 [ADMIN] 뿐인데
    // PAGE_PERMISSIONS['/chat'] 는 [ADMIN, TEAM_LEADER] 여서, TEAM_LEADER 는 권한이
    // 있는데도 URL 을 직접 치지 않으면 열 수 없었다. 두 표를 집합 단위로 맞춘다.
    for (const item of NAV) {
      const allowed = PAGE_PERMISSIONS[item.href];
      if (!allowed) continue; // 권한표에 없는 경로는 이 테스트의 대상이 아니다
      expect(
        [...item.roles].sort(),
        `${item.key}(${item.href}) 메뉴 역할과 PAGE_PERMISSIONS 가 어긋난다`,
      ).toEqual([...allowed].sort());
    }
  });

  it('points at page routes that actually exist', () => {
    for (const item of NAV) {
      const rel = item.href === '/' ? 'app/page.tsx' : `app${item.href}/page.tsx`;
      expect(() => readFileSync(join(SRC, rel)), `${item.href} → ${rel} 없음`).not.toThrow();
    }
  });
});
