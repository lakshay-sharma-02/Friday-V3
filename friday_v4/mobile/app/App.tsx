/**
 * Friday Companion — the phone as another surface of the same Friday.
 *
 * Four tabs:
 *   Status   — transport health, shared-session summary, server URL, push
 *   Chat     — the ONE presence thread, read + appended via the same brain
 *   Feed     — live ambient event stream (SSE durable queue)
 *   Devices  — pairing (one-time code) + paired-device management
 */
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { ApiClient, defaultBaseUrl } from './src/api';
import { useEvents } from './src/useEvents';
import { getPushToken } from './src/push';
import { theme } from './src/theme';
import { StatusScreen } from './src/screens/StatusScreen';
import { ChatScreen } from './src/screens/ChatScreen';
import { FeedScreen } from './src/screens/FeedScreen';
import { DevicesScreen } from './src/screens/DevicesScreen';

type Tab = 'status' | 'chat' | 'feed' | 'devices';

const STORAGE_KEY = 'friday.companion.baseUrl';
const TOKEN_KEY = 'friday.companion.token';

const TABS: { key: Tab; label: string }[] = [
  { key: 'status', label: 'Status' },
  { key: 'chat', label: 'Chat' },
  { key: 'feed', label: 'Feed' },
  { key: 'devices', label: 'Devices' },
];

export default function App() {
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('status');
  const [pushToken, setPushToken] = useState<string | null>(null);
  const { connected, events } = useEvents(baseUrl, 120, token);

  useEffect(() => {
    (async () => {
      try {
        const [savedUrl, savedToken] = await Promise.all([
          AsyncStorage.getItem(STORAGE_KEY),
          AsyncStorage.getItem(TOKEN_KEY),
        ]);
        setBaseUrl(savedUrl || defaultBaseUrl());
        setToken(savedToken || null);
      } catch {
        setBaseUrl(defaultBaseUrl());
      }
    })();
  }, []);

  useEffect(() => {
    if (baseUrl) {
      getPushToken().then((r) => setPushToken(r.ok ? (r.token ?? null) : null));
    }
  }, [baseUrl]);

  const api = baseUrl ? new ApiClient(baseUrl, token) : null;

  const saveBaseUrl = async (url: string) => {
    setBaseUrl(url);
    try {
      await AsyncStorage.setItem(STORAGE_KEY, url);
    } catch {
      // persistence is best-effort
    }
  };

  const saveToken = async (t: string) => {
    const next = t.trim() || null;
    setToken(next);
    try {
      if (next) {
        await AsyncStorage.setItem(TOKEN_KEY, next);
      } else {
        await AsyncStorage.removeItem(TOKEN_KEY);
      }
    } catch {
      // persistence is best-effort
    }
  };

  return (
    <View style={styles.root}>
      <StatusBar style="light" />
      <View style={styles.header}>
        <Text style={styles.brand}>FRIDAY</Text>
        <Text style={styles.tagline}>Companion · one presence, every surface</Text>
      </View>
      <View style={styles.body}>
        {api && tab === 'status' && (
          <StatusScreen
            api={api}
            baseUrl={baseUrl ?? ''}
            onSaveBaseUrl={saveBaseUrl}
            token={token ?? ''}
            onSaveToken={saveToken}
            pushToken={pushToken}
          />
        )}
        {api && tab === 'chat' && <ChatScreen api={api} />}
        {api && tab === 'feed' && <FeedScreen events={events} connected={connected} />}
        {api && tab === 'devices' && <DevicesScreen api={api} />}
      </View>
      <View style={styles.tabs}>
        {TABS.map((t) => (
          <Pressable
            key={t.key}
            style={[styles.tab, tab === t.key && styles.tabActive]}
            onPress={() => setTab(t.key)}
          >
            <Text style={[styles.tabLabel, tab === t.key && styles.tabLabelActive]}>
              {t.label}
            </Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: theme.bg,
    paddingTop: 52,
  },
  header: {
    paddingHorizontal: theme.spacing + 4,
    paddingBottom: theme.spacing,
    gap: 2,
  },
  brand: {
    color: theme.accent,
    fontSize: 24,
    fontWeight: '900',
    letterSpacing: 4,
  },
  tagline: { color: theme.textDim, fontSize: 12, letterSpacing: 0.4 },
  body: { flex: 1 },
  tabs: {
    flexDirection: 'row',
    borderTopWidth: 1,
    borderTopColor: theme.border,
    backgroundColor: theme.surface,
    paddingBottom: 14,
  },
  tab: {
    flex: 1,
    alignItems: 'center',
    paddingVertical: 12,
    borderTopWidth: 2,
    borderTopColor: 'transparent',
  },
  tabActive: { borderTopColor: theme.accent },
  tabLabel: { color: theme.textDim, fontSize: 12, fontWeight: '600' },
  tabLabelActive: { color: theme.accent },
});
