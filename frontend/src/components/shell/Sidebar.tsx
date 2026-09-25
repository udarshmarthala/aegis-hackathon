'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { PanelLeftClose, PanelLeft } from 'lucide-react';
import { cn } from '@/lib/utils';
import { AegisMark } from './AegisMark';
import { NAV_SECTIONS } from './navigation';

const STORAGE_KEY = 'aegis.sidebar.collapsed';

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  // Preference is remembered per operator (UX spec 88). Reading in an effect
  // keeps the server and first client render identical, avoiding hydration
  // mismatch.
  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(STORAGE_KEY) === '1');
    } catch {
      /* private mode or blocked storage - the default is fine */
    }
  }, []);

  function toggle() {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(STORAGE_KEY, next ? '1' : '0');
      } catch {
        /* non-fatal */
      }
      return next;
    });
  }

  return (
    <nav
      aria-label="Primary"
      className={cn(
        'flex shrink-0 flex-col border-r border-hairline bg-canvas transition-[width] duration-200',
        collapsed ? 'w-[68px]' : 'w-[236px]',
      )}
    >
      <div className="flex h-[54px] items-center gap-2.5 border-b border-hairline px-4">
        <AegisMark className="h-5 w-5 shrink-0" />
        {!collapsed && (
          <span className="text-body font-semibold tracking-tight">Aegis</span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto py-3">
        {NAV_SECTIONS.map((section) => (
          <div key={section.label} className="mb-4">
            {!collapsed && (
              <p className="px-4 pb-1.5 text-meta uppercase tracking-wider text-ink-tertiary">
                {section.label}
              </p>
            )}
            <ul className="space-y-0.5 px-2">
              {section.items.map((item) => {
                const active = pathname.startsWith(item.href);
                const Icon = item.icon;
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      title={collapsed ? item.label : undefined}
                      aria-current={active ? 'page' : undefined}
                      className={cn(
                        'flex items-center gap-2.5 rounded-btn px-2.5 py-1.5 text-body',
                        'transition-colors duration-hover',
                        active
                          ? 'bg-surface-3 text-ink-primary'
                          : 'text-ink-secondary hover:bg-surface-2 hover:text-ink-primary',
                      )}
                    >
                      <Icon className="h-[15px] w-[15px] shrink-0" aria-hidden />
                      {!collapsed && <span className="truncate">{item.label}</span>}
                      {active && !collapsed && (
                        <span className="ml-auto h-1 w-1 rounded-full bg-accent" aria-hidden />
                      )}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={toggle}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="flex items-center gap-2.5 border-t border-hairline px-4 py-2.5
                   text-meta text-ink-tertiary transition-colors duration-hover hover:text-ink-secondary"
      >
        {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
        {!collapsed && <span>Collapse</span>}
      </button>
    </nav>
  );
}
