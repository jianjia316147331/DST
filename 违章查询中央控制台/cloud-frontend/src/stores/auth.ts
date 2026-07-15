import { create } from 'zustand';
import api from '../api';

interface User {
  phone: string;
  name: string | null;
}

interface AuthState {
  user: User | null;
  token: string | null;
  loading: boolean;
  login: (phone: string) => Promise<void>;
  logout: () => void;
  checkAuth: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  token: localStorage.getItem('token'),
  loading: false,

  login: async (phone: string) => {
    const { data } = await api.post('/api/auth/login', { phone });
    localStorage.setItem('token', data.token);
    set({ token: data.token, user: data.user });
  },

  logout: () => {
    localStorage.removeItem('token');
    set({ token: null, user: null });
  },

  checkAuth: async () => {
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
      const { data } = await api.get('/api/auth/me');
      set({ user: data, token });
    } catch {
      localStorage.removeItem('token');
      set({ token: null, user: null });
    }
  },
}));
