/**
 * Devices — pair this phone with the desktop, and manage paired devices.
 *
 * Pairing is consent-first: ``friday4 mobile pair`` on the desktop prints
 * a 6-character one-time code (10-minute TTL). Enter it here alongside a
 * friendly name; the phone binds its Expo push token to the registry and
 * the daemon starts fanning ambient events to it.
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
import { ApiClient, DeviceInfo } from '../api';
import { getPushToken } from '../push';
import { theme } from '../theme';

interface Props {
  api: ApiClient;
}

export function DevicesScreen({ api }: Props) {
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [phase, setPhase] = useState<'idle' | 'working' | 'done'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [pushReady, setPushReady] = useState<boolean | null>(null);

  const refresh = useCallback(() => {
    api
      .devices()
      .then((d) => setDevices(d.devices))
      .catch(() => setDevices([]));
  }, [api]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  const pair = async () => {
    if (!code.trim() || phase === 'working') return;
    setPhase('working');
    setError(null);
    const push = await getPushToken();
    if (!push.ok || !push.token) {
      setError(push.error ?? 'Could not obtain a push token.');
      setPhase('idle');
      return;
    }
    setPushReady(true);
    const result = await api.registerDevice(code, name || 'my phone', push.token);
    if (result.ok) {
      setPhase('done');
      setCode('');
      setName('');
      refresh();
    } else {
      setError(result.error ?? 'Pairing failed — check the code and try again.');
      setPhase('idle');
    }
  };

  const unpair = async (id: string) => {
    await api.removeDevice(id).catch(() => undefined);
    refresh();
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <View style={styles.card}>
        <Text style={styles.cardTitle}>Pair this phone</Text>
        <Text style={styles.dim}>
          On the desktop run <Text style={styles.mono}>friday4 mobile pair</Text>,
          then enter the 6-character code here.
        </Text>
        <TextInput
          style={[styles.input, styles.codeInput]}
          value={code}
          onChangeText={(t) => setCode(t.toUpperCase().replace(/[^A-Z0-9]/g, ''))}
          placeholder="ABC123"
          placeholderTextColor={theme.textDim}
          autoCapitalize="characters"
          maxLength={6}
        />
        <TextInput
          style={styles.input}
          value={name}
          onChangeText={setName}
          placeholder="Device name (e.g. my phone)"
          placeholderTextColor={theme.textDim}
          maxLength={40}
        />
        <Pressable
          style={[styles.button, (!code.trim() || phase === 'working') && styles.buttonDisabled]}
          onPress={pair}
          disabled={!code.trim() || phase === 'working'}
        >
          {phase === 'working' ? (
            <ActivityIndicator color={theme.accentText} />
          ) : (
            <Text style={styles.buttonText}>
              {phase === 'done' ? 'Paired ✓' : 'Pair this phone'}
            </Text>
          )}
        </Pressable>
        {error ? <Text style={styles.error}>{error}</Text> : null}
        {pushReady === false ? (
          <Text style={styles.dim}>
            Push token unavailable — see the message above. Pairing still
            works once a token resolves.
          </Text>
        ) : null}
      </View>

      <View style={styles.card}>
        <Text style={styles.cardTitle}>Paired devices</Text>
        {devices.length === 0 ? (
          <Text style={styles.dim}>No devices paired yet.</Text>
        ) : (
          devices.map((d) => (
            <View key={d.id} style={styles.deviceRow}>
              <View style={styles.deviceInfo}>
                <Text style={styles.deviceName}>
                  {d.name || d.platform || 'phone'}
                </Text>
                <Text style={styles.dim}>
                  {d.platform ?? ''} · {d.last_seen ?? d.created_at ?? ''}
                </Text>
              </View>
              <Pressable style={styles.unpair} onPress={() => unpair(d.id)}>
                <Text style={styles.unpairText}>Unpair</Text>
              </Pressable>
            </View>
          ))
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
    gap: 10,
  },
  cardTitle: { color: theme.text, fontSize: 15, fontWeight: '700' },
  dim: { color: theme.textDim, fontSize: 13, lineHeight: 19 },
  mono: { color: theme.blue, fontFamily: 'monospace', fontSize: 12 },
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
  codeInput: { letterSpacing: 6, fontSize: 20, fontWeight: '800', textAlign: 'center' },
  button: {
    backgroundColor: theme.accent,
    borderRadius: theme.radiusSm,
    paddingVertical: 12,
    alignItems: 'center',
  },
  buttonDisabled: { opacity: 0.45 },
  buttonText: { color: theme.accentText, fontWeight: '800', fontSize: 14 },
  error: { color: theme.red, fontSize: 13, lineHeight: 18 },
  deviceRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  },
  deviceInfo: { flex: 1, gap: 2 },
  deviceName: { color: theme.text, fontWeight: '600', fontSize: 14 },
  unpair: {
    borderWidth: 1,
    borderColor: theme.red,
    borderRadius: theme.radiusSm,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  unpairText: { color: theme.red, fontSize: 12, fontWeight: '600' },
});
