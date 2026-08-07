'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useTranslations } from 'next-intl';
import {
  LayoutDashboard,
  KeyRound,
  Wallet,
  BrainCircuit,
  Gauge,
  Users,
  UserCircle,
  Activity,
  Sparkles,
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
    icon: <LayoutDashboard size={18} />,
    allowedRoles: [UserRoleConst.ADMIN, UserRoleConst.TEAM_LEADER, UserRoleConst.DEVELOPER],
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