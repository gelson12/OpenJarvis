// Widget store for the JARVIS on-screen UI.
//
// A small reducer + React context. Holds the open widgets, their
// positions and stacking order, the gesture highlight, and applies
// incoming `jarvis-ui` messages from the worker.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
  type ReactNode,
} from 'react';
import {
  WIDGET_DEFAULT_SIZE,
  WIDGET_DEFAULT_TITLE,
  type JarvisUIMessage,
  type WidgetInstance,
  type WidgetKind,
} from './protocol';

interface State {
  widgets: WidgetInstance[];
  spawnCount: number;
  zTop: number;
  /** Widget currently highlighted by the gesture cursor, if any. */
  highlightId: string | null;
}

type Action =
  | { t: 'open'; kind: WidgetKind; title?: string; payload?: unknown; id?: string }
  | { t: 'close'; id?: string; kind?: WidgetKind }
  | { t: 'close_all' }
  | { t: 'update'; id?: string; kind?: WidgetKind; payload?: unknown }
  | { t: 'focus'; id: string }
  | { t: 'cycle' }
  | { t: 'move'; id: string; x: number; y: number }
  | { t: 'highlight'; id: string | null };

/** Short unique id — crypto.randomUUID is available in all modern browsers. */
function newId(): string {
  return crypto.randomUUID().slice(0, 8);
}

/** Cascades each new widget so they don't stack exactly on top. */
function spawnPosition(n: number): { x: number; y: number } {
  return { x: 96 + (n % 6) * 48, y: 86 + (n % 6) * 42 };
}

function reducer(state: State, action: Action): State {
  switch (action.t) {
    case 'open': {
      const z = state.zTop + 1;
      // Widgets are singletons per kind — re-opening focuses + refreshes.
      const existing = state.widgets.find((w) => w.kind === action.kind);
      if (existing) {
        return {
          ...state,
          zTop: z,
          widgets: state.widgets.map((w) =>
            w.id === existing.id
              ? {
                  ...w,
                  z,
                  title: action.title ?? w.title,
                  payload: action.payload !== undefined ? action.payload : w.payload,
                }
              : w
          ),
        };
      }
      const size = WIDGET_DEFAULT_SIZE[action.kind];
      const pos = spawnPosition(state.spawnCount);
      const widget: WidgetInstance = {
        id: action.id ?? newId(),
        kind: action.kind,
        title: action.title ?? WIDGET_DEFAULT_TITLE[action.kind],
        payload: action.payload,
        x: pos.x,
        y: pos.y,
        w: size.w,
        h: size.h,
        z,
      };
      return {
        ...state,
        widgets: [...state.widgets, widget],
        spawnCount: state.spawnCount + 1,
        zTop: z,
      };
    }
    case 'close': {
      const gone = state.widgets.filter((w) =>
        action.id !== undefined ? w.id === action.id : w.kind === action.kind
      );
      const stillHighlighted = !gone.some((w) => w.id === state.highlightId);
      return {
        ...state,
        highlightId: stillHighlighted ? state.highlightId : null,
        widgets: state.widgets.filter((w) =>
          action.id !== undefined ? w.id !== action.id : w.kind !== action.kind
        ),
      };
    }
    case 'close_all':
      return { ...state, widgets: [], highlightId: null };
    case 'update':
      return {
        ...state,
        widgets: state.widgets.map((w) =>
          (action.id !== undefined && w.id === action.id) ||
          (action.kind !== undefined && w.kind === action.kind)
            ? { ...w, payload: action.payload }
            : w
        ),
      };
    case 'focus': {
      const z = state.zTop + 1;
      return {
        ...state,
        zTop: z,
        widgets: state.widgets.map((w) => (w.id === action.id ? { ...w, z } : w)),
      };
    }
    case 'cycle': {
      // Bring the bottom-most widget to the front — cycles the stack.
      if (state.widgets.length < 2) return state;
      let bottom = state.widgets[0];
      for (const w of state.widgets) if (w.z < bottom.z) bottom = w;
      const z = state.zTop + 1;
      return {
        ...state,
        zTop: z,
        widgets: state.widgets.map((w) => (w.id === bottom.id ? { ...w, z } : w)),
      };
    }
    case 'move':
      return {
        ...state,
        widgets: state.widgets.map((w) =>
          w.id === action.id ? { ...w, x: action.x, y: action.y } : w
        ),
      };
    case 'highlight':
      return state.highlightId === action.id
        ? state
        : { ...state, highlightId: action.id };
    default:
      return state;
  }
}

export interface JarvisUIContextValue {
  widgets: WidgetInstance[];
  highlightId: string | null;
  open: (kind: WidgetKind, opts?: { title?: string; payload?: unknown }) => void;
  close: (id: string) => void;
  closeByKind: (kind: WidgetKind) => void;
  closeAll: () => void;
  focus: (id: string) => void;
  cycleFocus: () => void;
  move: (id: string, x: number, y: number) => void;
  setHighlight: (id: string | null) => void;
  /** Apply a raw worker message (used by useJarvisUIChannel). */
  applyMessage: (msg: JarvisUIMessage) => void;
}

const JarvisUIContext = createContext<JarvisUIContextValue | null>(null);

export function JarvisUIProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, {
    widgets: [],
    spawnCount: 0,
    zTop: 0,
    highlightId: null,
  });

  const open = useCallback(
    (kind: WidgetKind, opts?: { title?: string; payload?: unknown }) =>
      dispatch({ t: 'open', kind, title: opts?.title, payload: opts?.payload }),
    []
  );
  const close = useCallback((id: string) => dispatch({ t: 'close', id }), []);
  const closeByKind = useCallback(
    (kind: WidgetKind) => dispatch({ t: 'close', kind }),
    []
  );
  const closeAll = useCallback(() => dispatch({ t: 'close_all' }), []);
  const focus = useCallback((id: string) => dispatch({ t: 'focus', id }), []);
  const cycleFocus = useCallback(() => dispatch({ t: 'cycle' }), []);
  const move = useCallback(
    (id: string, x: number, y: number) => dispatch({ t: 'move', id, x, y }),
    []
  );
  const setHighlight = useCallback(
    (id: string | null) => dispatch({ t: 'highlight', id }),
    []
  );
  const applyMessage = useCallback((msg: JarvisUIMessage) => {
    switch (msg.type) {
      case 'open_widget':
        dispatch({
          t: 'open',
          kind: msg.kind,
          title: msg.title,
          payload: msg.payload,
          id: msg.id,
        });
        break;
      case 'close_widget':
        dispatch({ t: 'close', id: msg.id, kind: msg.kind });
        break;
      case 'update_widget':
        dispatch({ t: 'update', id: msg.id, kind: msg.kind, payload: msg.payload });
        break;
      case 'focus_widget':
        if (msg.id) dispatch({ t: 'focus', id: msg.id });
        break;
      case 'close_all':
        dispatch({ t: 'close_all' });
        break;
    }
  }, []);

  const value = useMemo<JarvisUIContextValue>(
    () => ({
      widgets: state.widgets,
      highlightId: state.highlightId,
      open,
      close,
      closeByKind,
      closeAll,
      focus,
      cycleFocus,
      move,
      setHighlight,
      applyMessage,
    }),
    [
      state.widgets,
      state.highlightId,
      open,
      close,
      closeByKind,
      closeAll,
      focus,
      cycleFocus,
      move,
      setHighlight,
      applyMessage,
    ]
  );

  return <JarvisUIContext.Provider value={value}>{children}</JarvisUIContext.Provider>;
}

export function useJarvisUI(): JarvisUIContextValue {
  const ctx = useContext(JarvisUIContext);
  if (!ctx) {
    throw new Error('useJarvisUI must be used within a JarvisUIProvider');
  }
  return ctx;
}
