import { createContext, useContext, useReducer, useEffect } from 'react';
import {
  INITIAL_CHORES,
  INITIAL_SHOPPING_ITEMS,
  INITIAL_MEALS,
  INITIAL_EVENTS,
} from '../data/seedData.js';

const DataContext = createContext();

const STORAGE_KEY = 'huddle-data';

const defaultState = {
  chores: INITIAL_CHORES,
  shoppingItems: INITIAL_SHOPPING_ITEMS,
  meals: INITIAL_MEALS,
  events: INITIAL_EVENTS,
  nextId: 100,
};

function loadState() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? JSON.parse(stored) : defaultState;
  } catch {
    return defaultState;
  }
}

function reducer(state, action) {
  switch (action.type) {
    case 'TOGGLE_CHORE':
      return {
        ...state,
        chores: state.chores.map((c) =>
          c.id === action.id
            ? { ...c, status: c.status === 'completed' ? 'due' : 'completed' }
            : c,
        ),
      };

    case 'ADD_CHORE':
      return {
        ...state,
        nextId: state.nextId + 1,
        chores: [
          ...state.chores,
          {
            id: state.nextId,
            name: action.name,
            person: action.person,
            schedule: action.schedule,
            status: 'due',
            streak: 0,
          },
        ],
      };

    case 'DELETE_CHORE':
      return {
        ...state,
        chores: state.chores.filter((c) => c.id !== action.id),
      };

    case 'TOGGLE_SHOPPING_ITEM':
      return {
        ...state,
        shoppingItems: state.shoppingItems.map((item) =>
          item.id === action.id
            ? { ...item, purchased: !item.purchased }
            : item,
        ),
      };

    case 'ADD_SHOPPING_ITEM':
      return {
        ...state,
        nextId: state.nextId + 1,
        shoppingItems: [
          ...state.shoppingItems,
          {
            id: state.nextId,
            name: action.name,
            quantity: action.quantity,
            category: action.category,
            purchased: false,
            inPantry: false,
          },
        ],
      };

    case 'DELETE_SHOPPING_ITEM':
      return {
        ...state,
        shoppingItems: state.shoppingItems.filter((item) => item.id !== action.id),
      };

    case 'CLEAR_PURCHASED':
      return {
        ...state,
        shoppingItems: state.shoppingItems.filter((item) => !item.purchased),
      };

    case 'UPDATE_MEAL':
      return {
        ...state,
        meals: state.meals.map((m) =>
          m.day === action.day ? { ...m, [action.field]: action.value } : m,
        ),
      };

    case 'ADD_EVENT':
      return {
        ...state,
        nextId: state.nextId + 1,
        events: [
          ...state.events,
          {
            id: state.nextId,
            title: action.title,
            time: action.time,
            person: action.person,
            duration: action.duration,
            color: action.color,
          },
        ],
      };

    case 'DELETE_EVENT':
      return {
        ...state,
        events: state.events.filter((e) => e.id !== action.id),
      };

    default:
      return state;
  }
}

export function DataProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, null, loadState);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }, [state]);

  return (
    <DataContext.Provider value={{ state, dispatch }}>
      {children}
    </DataContext.Provider>
  );
}

export function useData() {
  const ctx = useContext(DataContext);
  if (!ctx) throw new Error('useData must be used within a DataProvider');
  return ctx;
}
