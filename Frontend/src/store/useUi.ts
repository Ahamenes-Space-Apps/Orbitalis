/**
 * Local UI state only (UX §14.1). Navigation, selection, frame, view, system,
 * exaggeration and layers live in the URL; everything here is ephemeral.
 */

import { create } from 'zustand';
import type { SkyPos } from '@/lib/coords';

export type CandidateSort = 'rank' | 'rate' | 'ordinal';
export type Theme = 'dark' | 'light';

const THEME_KEY = 'orbitalis-theme';

export function readStoredTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  try {
    return window.localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.toggle('dark', theme === 'dark');
  root.style.colorScheme = theme;
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage can be unavailable in private modes */
  }
}

interface UiState {
  theme: Theme;
  queueCollapsed: boolean;
  drawerOpen: boolean;
  fieldEntryOpen: boolean;
  playing: boolean;
  speed: number;
  showFlags: boolean;
  showGrid: boolean;
  epochCollapsed: boolean;
  trayCollapsed: boolean;
  sortBy: CandidateSort;
  hoveredCandidateId: string | null;
  cursor: SkyPos | null;

  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  toggleQueue: () => void;
  setDrawerOpen: (open: boolean) => void;
  setFieldEntryOpen: (open: boolean) => void;
  setPlaying: (playing: boolean) => void;
  togglePlay: () => void;
  setSpeed: (speed: number) => void;
  setShowFlags: (show: boolean) => void;
  setShowGrid: (show: boolean) => void;
  setEpochCollapsed: (collapsed: boolean) => void;
  setTrayCollapsed: (collapsed: boolean) => void;
  setSortBy: (sort: CandidateSort) => void;
  setHoveredCandidateId: (id: string | null) => void;
  setCursor: (position: SkyPos | null) => void;
}

export const useUi = create<UiState>((set) => ({
  theme: readStoredTheme(),
  queueCollapsed: false,
  drawerOpen: false,
  fieldEntryOpen: false,
  playing: false,
  speed: 2,
  showFlags: false,
  showGrid: true,
  epochCollapsed: false,
  trayCollapsed: false,
  sortBy: 'rank',
  hoveredCandidateId: null,
  cursor: null,

  setTheme: (theme) => {
    applyTheme(theme);
    set({ theme });
  },
  toggleTheme: () =>
    set((state) => {
      const theme = state.theme === 'dark' ? 'light' : 'dark';
      applyTheme(theme);
      return { theme };
    }),
  toggleQueue: () => set((state) => ({ queueCollapsed: !state.queueCollapsed })),
  setDrawerOpen: (drawerOpen) => set({ drawerOpen }),
  setFieldEntryOpen: (fieldEntryOpen) => set({ fieldEntryOpen }),
  setPlaying: (playing) => set({ playing }),
  togglePlay: () => set((state) => ({ playing: !state.playing })),
  setSpeed: (speed) => set({ speed }),
  setShowFlags: (showFlags) => set({ showFlags }),
  setShowGrid: (showGrid) => set({ showGrid }),
  setEpochCollapsed: (epochCollapsed) => set({ epochCollapsed }),
  setTrayCollapsed: (trayCollapsed) => set({ trayCollapsed }),
  setSortBy: (sortBy) => set({ sortBy }),
  setHoveredCandidateId: (hoveredCandidateId) => set({ hoveredCandidateId }),
  setCursor: (cursor) => set({ cursor }),
}));
