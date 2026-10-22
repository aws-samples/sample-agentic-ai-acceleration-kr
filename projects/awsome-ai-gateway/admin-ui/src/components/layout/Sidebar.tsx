'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useTranslations } from 'next-intl';
import {
  Activity,
  AppWindow,
  BrainCircuit,
  Gauge,
  KeyRound,
  LayoutDashboard,
  Sparkles,
  UserCircle,
  Users,
  Wallet,
} from 'lucide-react';
import type { UserRole } from '@/types/enums';
import { UserRole as UserRoleConst } from '@/types/enums';
import { ThemeToggle } from '@/components/common/ThemeToggle';
import { BrandLogo } from '@/components/brand/BrandLogo';

interface SidebarProps {
  role?: UserRole;
}

interface NavItemDef {
  key: string; // translation key in 'nav' namespace
  href: string;
  icon: React.ReactNode;
  allowedRoles: UserRole[];
}

const NAV_ITEMS: NavItemDef[] = [
  {
    key: 'dashboard',
    href: '/',
    // DEVELOPER 제외 — PAGE_PERMISSIONS['/'] 가 ADMIN/TEAM_LEADER 뿐이라, 예전엔
    // DEVELOPER 가 /my 에서 이 메뉴를 보고 눌러도 미들웨어가 /403 으로 튕겼다
    // (페이지 접근 권한은 그대로다. 못 여는 링크를 안 보여 주는 것뿐).
    allowedRoles: [UserRoleConst.ADMIN, UserRoleConst.TEAM_LEADER],
    icon: <LayoutDashboard size={18} />,
  },
  {
    key: 'users',
    href: '/users',
    icon: <Users size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'models',
    href: '/models',
    icon: <BrainCircuit size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    // 앱(클라이언트) 정책 — allowedRoles 는 PAGE_PERMISSIONS['/apps'] 와 **정확히**
    // 같아야 한다. 좁으면 메뉴에서 안 보이고, 넓으면 보이지만 클릭하면 /403 이다.
    key: 'apps',
    href: '/apps',
    icon: <AppWindow size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'budgets',
    href: '/budgets',
    icon: <Wallet size={18} />,
    allowedRoles: [UserRoleConst.ADMIN, UserRoleConst.TEAM_LEADER],
  },
  {
    key: 'rateLimits',
    href: '/rate-limits',
    icon: <Gauge size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'keys',
    href: '/keys',
    icon: <KeyRound size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'monitoring',
    href: '/monitoring',
    icon: <Activity size={18} />,
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'chat',
    href: '/chat',
    icon: <Sparkles size={18} />,
    // ADMIN 전용 — chat_agent 라우터의 8개 엔드포인트가 전부
    // Depends(require_admin) 이다(admin-api/src/app/routers/chat_agent.py:78,122,182,
    // 211,324,385,756,841). 한때 PAGE_PERMISSIONS 를 근거로 TEAM_LEADER 까지 넓혔었는데,
    // 그러면 메뉴는 보이지만 페이지의 모든 호출이 403 이 되는 죽은 화면이 된다.
    // 권한표 쪽을 백엔드에 맞춰 좁히는 것이 맞다.
    allowedRoles: [UserRoleConst.ADMIN],
  },
  {
    key: 'my',
    href: '/my',
    icon: <UserCircle size={18} />,
    allowedRoles: [UserRoleConst.TEAM_LEADER, UserRoleConst.DEVELOPER],
  },
];

export function Sidebar({ role }: SidebarProps) {
  const pathname = usePathname();
  const t = useTranslations('nav');

  const visibleItems = role
    ? NAV_ITEMS.filter((item) => item.allowedRoles.includes(role))
    : NAV_ITEMS.filter((item) => item.href === '/');

  const isActive = (href: string): boolean => {
    if (href === '/') return pathname === '/';
    return pathname === href || pathname.startsWith(`${href}/`);
  };

  return (
    <aside className="w-64 flex-shrink-0 flex flex-col border-r border-sidebar-border bg-sidebar-background">
      {/* Logo / Brand — 고스트 그라데이션 마크(BrandLogo 단일 출처). ink 타일 위에
          teal 그라데이션 고스트가 두 테마 모두에서 선명. */}
      <div className="flex h-16 items-center gap-2 border-b border-sidebar-border px-6">
        <div className="flex h-8 w-8 items-center justify-center rounded-apple-sm bg-[#1b2430]">
          <BrandLogo size={21} idSuffix="sidebar" />
        </div>
        <span className="text-sm font-semibold text-sidebar-foreground">AWSome AI Gateway</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4 px-3" aria-label={t('mainMenu')}>
        <ul className="flex flex-col gap-0.5" role="list">
          {visibleItems.map((item) => {
            const active = isActive(item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={[
                    'relative flex items-center gap-3 rounded-apple-sm px-3 py-2 text-sm font-medium pressable transition-[background,color,box-shadow] duration-150',
                    active
                      ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)] before:absolute before:left-0 before:top-[22%] before:bottom-[22%] before:w-[3px] before:rounded-full before:bg-primary before:content-[""] dark:before:shadow-[0_0_10px_hsl(var(--primary))]'
                      : 'text-sidebar-foreground hover:bg-primary/[0.06] hover:text-primary dark:hover:bg-white/[0.05] dark:hover:text-foreground',
                  ].join(' ')}
                  aria-current={active ? 'page' : undefined}
                >
                  <span className="flex-shrink-0" aria-hidden="true">
                    {item.icon}
                  </span>
                  {t(item.key)}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Footer */}
      <div className="flex flex-col gap-3 border-t border-sidebar-border px-4 py-3">
        <ThemeToggle />
        <p className="px-2 text-xs text-muted-foreground">AWSome AI Gateway Admin</p>
      </div>
    </aside>
  );
}