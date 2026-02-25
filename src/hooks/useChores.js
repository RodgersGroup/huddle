import { useMemo, useCallback } from 'react';
import { useData } from '../context/DataContext.jsx';

export function useChores() {
  const { state, dispatch } = useData();
  const { chores } = state;

  const toggleChore = useCallback(
    (id) => dispatch({ type: 'TOGGLE_CHORE', id }),
    [dispatch],
  );

  const addChore = useCallback(
    (name, person, schedule) =>
      dispatch({ type: 'ADD_CHORE', name, person, schedule }),
    [dispatch],
  );

  const deleteChore = useCallback(
    (id) => dispatch({ type: 'DELETE_CHORE', id }),
    [dispatch],
  );

  const filtered = useCallback(
    (key) => (key === 'all' ? chores : chores.filter((c) => c.status === key)),
    [chores],
  );

  const dueToday = useMemo(
    () => chores.filter((c) => c.status === 'due' || c.status === 'overdue'),
    [chores],
  );

  const overdueCount = useMemo(
    () => chores.filter((c) => c.status === 'overdue').length,
    [chores],
  );

  return { chores, toggleChore, addChore, deleteChore, filtered, dueToday, overdueCount };
}
