import { useState, useEffect } from 'react';
import { api } from '../utils';

/**
 * useTaskEvents - Hook for fetching task events when modal opens
 */
export function useTaskEvents(executionName: string | null | undefined, isModalOpen: boolean) {
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(false);
    
    useEffect(() => {
        if (!executionName || !isModalOpen) {
            return;
        }
        
        let isMounted = true;
        
        const fetchEvents = async () => {
            setLoading(true);
            try {
                const resp = await api.get(`/task-events?name=${encodeURIComponent(executionName)}`);
                if (isMounted) {
                    if (resp && resp.events) {
                        setEvents(resp.events);
                    } else {
                        setEvents([]);
                    }
                }
            } catch (e) {
                console.error('Failed to fetch task events:', e);
                if (isMounted) {
                    setEvents([]);
                }
            } finally {
                if (isMounted) {
                    setLoading(false);
                }
            }
        };
        
        fetchEvents();
        
        return () => {
            isMounted = false;
        };
    }, [executionName, isModalOpen]);
    
    return { events, loading };
}
