'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import {
  getTeamAllowedModelsAction,
  setTeamAllowedModelsAction,
  clearTeamAllowedModelsAction,
} from '@/lib/actions/models';
import type { ModelListItem } from '@/types/entities';

interface TeamOption {
  id: string;
  name: string;
}

interface TeamModelPermissionPanelProps {
  teams: TeamOption[];
  allTeams?: TeamOption[];
  models: ModelListItem[];
}

export function TeamModelPermissionPanel({ teams, allTeams, models }: TeamModelPermissionPanelProps) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [selectedTeamId, setSelectedTeamId] = useState('');
  const [allowedAliases, setAllowedAliases] = useState<string[]>([]);
  const [hasRestrictions, setHasRestrictions] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [showInactive, setShowInactive] = useState(false);

  const visibleTeams = showInactive && allTeams ? allTeams : teams;

  const activeModels = models.filter(m => m.is_active);

  useEffect(() => {
    if (!selectedTeamId) {
      setAllowedAliases([]);
      setHasRestrictions(false);
      setLoaded(false);
      return;
    }
    startTransition(async () => {
      const result = await getTeamAllowedModelsAction(selectedTeamId);
      if (result.success) {
        setAllowedAliases(result.data.model_aliases);
        setHasRestrictions(result.data.model_aliases.length > 0);
      } else {
        setAllowedAliases([]);
        setHasRestrictions(false);
      }
      setLoaded(true);
    });
  }, [selectedTeamId]);

  const toggleModel = (alias: string) => {
    setAllowedAliases(prev =>
      prev.includes(alias) ? prev.filter(a => a !== alias) : [...prev, alias]
    );
  };

  const handleSave = () => {
    if (!selectedTeamId) return;
    if (allowedAliases.length === 0) {
      toast({ type: 'error', message: t('minOneModel'), auto_dismiss_ms: 3000 });
      return;
    }
    startTransition(async () => {
      const result = await setTeamAllowedModelsAction(selectedTeamId, allowedAliases);
      if (result.success) {
        setHasRestrictions(true);
        toast({ type: 'success', message: t('teamPermissionSaved'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  const handleClear = () => {
    if (!selectedTeamId) return;
    startTransition(async () => {
      const result = await clearTeamAllowedModelsAction(selectedTeamId);
      if (result.success) {
        setAllowedAliases([]);
        setHasRestrictions(false);
        toast({ type: 'success', message: t('restrictionCleared'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  return (
    <div className="space-y-4 glass rounded-apple p-4">
      <div className="flex items-center gap-4">
        <label className="text-sm font-medium">{t('selectTeam')}</label>
        <select
          value={selectedTeamId}
          onChange={e => setSelectedTeamId(e.target.value)}
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
        >
          <option value="">{t('selectTeamPlaceholder')}</option>
          {visibleTeams.map(t => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
        {allTeams && allTeams.length > teams.length && (
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            {t('includeInactiveTeams')}
          </label>
        )}
        {selectedTeamId && loaded && (
          hasRestrictions ? (
            <span className="badge badge-amber">{t('restrictionApplied')}</span>
          ) : (
            <span className="badge badge-teal">{t('noRestriction')}</span>
          )
        )}
      </div>

      {selectedTeamId && loaded && (
        <>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">{t('selectAllowedModels')}</span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setAllowedAliases(activeModels.map(m => m.alias))}
                  className="text-xs text-primary hover:underline"
                >
                  {t('selectAll')}
                </button>
                <button
                  type="button"
                  onClick={() => setAllowedAliases([])}
                  className="text-xs text-muted-foreground hover:underline"
                >
                  {t('clearAll')}
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
              {activeModels.map(m => (
                <label key={m.alias} className="flex items-center gap-2 rounded-md border p-2 cursor-pointer hover:bg-muted/50">
                  <input
                    type="checkbox"
                    checked={allowedAliases.includes(m.alias)}
                    onChange={() => toggleModel(m.alias)}
                    className="h-4 w-4 rounded border-gray-300"
                  />
                  <span className="text-sm">{m.alias}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-3 pt-2 border-t">
            <SpinnerButton
              onClick={handleSave}
              isLoading={isPending}
              className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-md text-sm font-medium"
            >
              {t('savePermission')}
            </SpinnerButton>
            {hasRestrictions && (
              <button
                type="button"
                onClick={handleClear}
                disabled={isPending}
                className="text-sm text-destructive hover:underline disabled:opacity-50"
              >
                {t('clearRestriction')}
              </button>
            )}
          </div>
        </>
      )}

      {selectedTeamId && !loaded && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          {t('loadingText')}
        </div>
      )}
    </div>
  );
}