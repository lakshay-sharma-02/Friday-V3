/**
 * Status — transport health + the one-presence thread summary, plus the
 * server address editor (persisted) and push registration state.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { ApiClient, StatusPayload } from '../api';
import { theme } from '../theme';

interface Props {
  api: ApiClient;
  baseUrl: string;
  onSaveBaseUrl: (url: string) => void;
  token: string;
  onSaveToken: (token: string) => void;
  pushToken: string | null;
}

export function StatusScreen({
  api,
  baseUrl,
  onSaveBaseUrl,
  token,
  onSaveToken,
  pushToken,
}: Props) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [host, setHost] = useState(baseUrl);
  const [tok, setTok] = useState(token);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setHost(baseUrl);
  }, [baseUrl]);

  useEffect(() => {
    setTok(token);
  }, [token]);

  const refresh = useCallback(() => {
    setBusy(true);
    api
      .status()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setBusy(false));
  }, [api]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  const session = status?.shared_session;
  const connected = status?.available === true;

  const save = () => {
    const url = host.trim();
    if (!url) return;
    onSaveBaseUrl(url);
    setSaved(true);
    setTimeout(() => setSaved(false), 1600);
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <View style={styles.card}>
        <View style={styles.rowBetween}>
          <Text style={styles.cardTitle}>Server</Text>
          <View style={[styles.pill, connected ? styles.pillOn : styles.pillOff]}>
            <Text style={styles.pillText}>{connected ? 'connected' : 'offline'}</Text>
          </View>
        </View>
        <Text style={styles.dim}>
          The desktop&apos;s Friday V4 mobile API — same brain as talk, voice
          and web.
        </Text>
        <TextInput
          style={styles.input}
          value={host}
          onChangeText={setHost}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          placeholder="http://192.168.1.20:8900"
          placeholderTextColor={theme.textDim}
        />
        <Pressable style={styles.button} onPress={save}>
          <Text style={styles.buttonText}>{saved ? 'Saved ✓' : 'Save server'}</Text>
        </Pressable>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Server token</Text>
        <Text style={styles.dim}>
          Optional — required once Friday is exposed beyond the LAN (set
          with <Text style={styles.mono}>friday4 mobile serve --token …</Text> on
          the desktop). The API is open on your Wi-Fi by default.
        </Text>
        <TextInput
          style={styles.input}
          value={tok}
          onChangeText={setTok}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          placeholder="companion token"
          placeholderTextColor={theme.textDim}
        />
        <Pressable style={styles.button} onPress={() => onSaveToken(tok)}>
          <Text style={styles.buttonText}>Save token</Text>
        </Pressable>
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Shared session</Text>
        {busy && !status ? (
          <ActivityIndicator color={theme.accent} />
        ) : session ? (
          <>
            <Text style={styles.dim}>
              Started on <Text style={styles.strong}>{session.surface ?? 'unknown surface'}</Text>
              {'\n'}— the conversation continues here.
            </Text>
            <View style={styles.metricRow}>
              <Text style={styles.metric}>{status?.exchanges_today ?? 0}</Text>
              <Text style={styles.dim}>exchanges today</Text>
            </View>
          </>
        ) : (
          <Text style={styles.dim}>
            No conversation yet. Ask something in the Chat tab — or in the
            terminal, on the web dashboard, or by voice — and it lands in the
            same thread.
          </Text>
        )}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Push</Text>
        {pushToken ? (
          <>
            <Text style={styles.dim}>Push registered — the daemon will ping you.</Text>
            <Text numberOfLines={1} style={styles.mono}>
              {pushToken.slice(0, 28)}…
            </Text>
          </>
        ) : (
          <Text style={styles.dim}>
            Not paired for push yet. Open the Devices tab, run{' '}
            <Text style={styles.mono}>friday4 mobile pair</Text> on the
            desktop, and enter the code.
          </Text>
        )}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  content: { padding: theme.spacing, gap: theme.spacing },
  card: {
    backgroundColor: theme.surface,
    borderRadius: theme.radius,
    borderWidth: 1,
    borderColor: theme.border,
    padding: theme.spacing + 4,
    gap: 8,
  },
  cardTitle: {
    color: theme.text,
    fontSize: 15,
    fontWeight: '700',
    letterSpacing: 0.3,
  },
  rowBetween: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  pill: {
    paddingHorizontal: 10,
    paddingVertical: 3,
    borderRadius: 999,
    borderWidth: 1,
  },
  pillOn: { backgroundColor: 'rgba(62,207,142,0.15)', borderColor: theme.green },
  pillOff: { backgroundColor: 'rgba(255,92,92,0.12)', borderColor: theme.red },
  pillText: { color: theme.text, fontSize: 12, fontWeight: '600' },
  dim: { color: theme.textDim, fontSize: 13, lineHeight: 19 },
  strong: { color: theme.text, fontWeight: '600' },
  input: {
    backgroundColor: theme.surfaceAlt,
    borderRadius: theme.radiusSm,
    borderWidth: 1,
    borderColor: theme.border,
    color: theme.text,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
  },
  button: {
    backgroundColor: theme.accent,
    borderRadius: theme.radiusSm,
    paddingVertical: 10,
    alignItems: 'center',
  },
  buttonText: { color: theme.accentText, fontWeight: '700', fontSize: 14 },
  metricRow: { flexDirection: 'row', alignItems: 'baseline', gap: 8 },
  metric: { color: theme.accent, fontSize: 26, fontWeight: '800' },
  mono: { color: theme.blue, fontFamily: 'monospace', fontSize: 12, marginTop: 2 },
});
