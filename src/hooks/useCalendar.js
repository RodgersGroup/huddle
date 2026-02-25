import { useCallback } from 'react';
import { useData } from '../context/DataContext.jsx';

export function useCalendar() {
  const { state, dispatch } = useData();

  const addEvent = useCallback(
    (title, time, person, duration, color) =>
      dispatch({ type: 'ADD_EVENT', title, time, person, duration, color }),
    [dispatch],
  );

  const deleteEvent = useCallback(
    (id) => dispatch({ type: 'DELETE_EVENT', id }),
    [dispatch],
  );

  return { events: state.events, addEvent, deleteEvent };
}
