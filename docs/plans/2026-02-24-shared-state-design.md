# Huddle — Shared State & Interactive Wiring Design

## Date: 2026-02-24

## Overview
Restructure Huddle from a single static JSX mockup into a proper Vite + React + Tailwind project with shared state, functional interactivity, and a data layer designed for future Cloudflare D1 backend.

## Architecture

### Project Structure
```
huddle/
├── src/
│   ├── app/HuddleApp.jsx           # Root: providers, navigation
│   ├── context/
│   │   ├── ThemeContext.jsx          # isDark, toggleTheme, localStorage persistence
│   │   └── DataContext.jsx           # All app data + dispatch (useReducer)
│   ├── hooks/
│   │   ├── useChores.js
│   │   ├── useShoppingList.js
│   │   ├── useMeals.js
│   │   └── useCalendar.js
│   ├── screens/
│   │   ├── HomeScreen.jsx
│   │   ├── ChoresScreen.jsx
│   │   ├── CalendarScreen.jsx
│   │   ├── ShoppingScreen.jsx
│   │   ├── MealsScreen.jsx
│   │   └── PlaceholderScreen.jsx
│   ├── components/
│   │   ├── PersonBadge.jsx
│   │   ├── StatusPill.jsx
│   │   ├── ThemeToggle.jsx
│   │   ├── ScreenHeader.jsx
│   │   ├── AddButton.jsx
│   │   └── BottomNav.jsx
│   ├── data/
│   │   ├── members.js
│   │   └── seedData.js
│   ├── theme/themes.js
│   ├── main.jsx
│   └── index.css
├── index.html
├── vite.config.js
└── package.json
```

### Shared State (DataContext + useReducer)
Single context holding all module data. Domain hooks wrap context for clean API:
- `useChores()` → { chores, toggleChore, addChore, deleteChore, filtered }
- `useShoppingList()` → { items, toggleItem, addItem, deleteItem, clearPurchased }
- `useMeals()` → { meals, updateMeal }
- `useCalendar()` → { events, addEvent }

All state persisted to localStorage. Hook internals swappable for backend later.

### ThemeContext
- isDark + toggleTheme in context (no prop drilling)
- localStorage persistence
- ThemeToggle self-contained

### Wired Buttons
- Chores AddButton → inline add form
- Calendar AddButton → inline add form
- Meals Edit → tappable meal slots
- Shopping "Add item" → real input
- Shopping "Clear all" → clears with confirmation
- Chore complete → toggleable

### Home Screen
- "Up Next" reads from useChores() real data
- Tile subtitles computed from real counts

### Bottom Nav
- Persistent tab bar: Home, Chores, Calendar, Meals, Shopping
- Direct navigation between screens

## Tech Stack
- React 19 + Vite
- Tailwind CSS 4
- React Context + useReducer
- localStorage (Phase 1) → Cloudflare D1 (Phase 2)

## Members
- Keiran (#4ecdc4), Ciara (#ff6b9d), Tahni (#4caf50)
