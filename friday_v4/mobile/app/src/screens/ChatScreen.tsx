/**
 * Chat — the one-presence conversation, on the phone.
 *
 * Reads the SAME shared session the terminal / web dashboard / voice
 * append to, and appends through the same nl_router brain. What you
 * asked Friday this morning in the terminal is here — and the MCU test
 * ("what did we talk about this morning?") is answerable from any
 * surface because it is all one thread.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { ApiClient, Exchange } from '../api';
import { theme } from '../theme';

interface Props {
  api: ApiClient;
}

interface Message {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  intent?: string;
}

export function ChatScreen({ api }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    api
      .conversation(40)
      .then((conv) => {
        setMessages(
          conv.exchanges.map((ex: Exchange, i: number) => ({
            key: `${i}-${ex.created_at ?? ''}`,
            role: ex.role === 'user' ? 'user' : 'assistant',
            content: ex.content,
            intent: ex.intent,
          })),
        );
      })
      .catch(() => {
        // server unreachable — keep whatever we have
      })
      .finally(() => setLoading(false));
  }, [api]);

  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);

  const send = () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');
    setSending(true);
    setMessages((m) => [
      ...m,
      { key: `u-${Date.now()}`, role: 'user', content: text },
    ]);
    api
      .talk(text)
      .then((result) => {
        setMessages((m) => [
          ...m,
          {
            key: `a-${Date.now()}`,
            role: 'assistant',
            content: result.response,
            intent: result.intent,
          },
        ]);
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        setMessages((m) => [
          ...m,
          {
            key: `a-${Date.now()}`,
            role: 'assistant',
            content: `Friday is unreachable: ${msg}`,
          },
        ]);
      })
      .finally(() => setSending(false));
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator color={theme.accent} />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <FlatList
        data={messages}
        keyExtractor={(item) => item.key}
        contentContainerStyle={styles.list}
        renderItem={({ item }) => (
          <View
            style={[
              styles.bubble,
              item.role === 'user' ? styles.bubbleUser : styles.bubbleAssistant,
            ]}
          >
            {item.intent ? (
              <Text style={styles.intent}>{item.intent}</Text>
            ) : null}
            <Text style={styles.bubbleText}>{item.content}</Text>
          </View>
        )}
      />
      <View style={styles.composer}>
        <TextInput
          style={styles.input}
          value={input}
          onChangeText={setInput}
          placeholder="Ask Friday anything…"
          placeholderTextColor={theme.textDim}
          multiline
          onSubmitEditing={send}
        />
        <Pressable
          style={[styles.send, !input.trim() && styles.sendDisabled]}
          onPress={send}
          disabled={!input.trim() || sending}
        >
          <Text style={styles.sendText}>{sending ? '…' : '➤'}</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  list: { padding: theme.spacing, gap: 8 },
  bubble: {
    maxWidth: '86%',
    borderRadius: theme.radius,
    paddingHorizontal: 12,
    paddingVertical: 9,
  },
  bubbleUser: {
    alignSelf: 'flex-end',
    backgroundColor: theme.accent,
    borderBottomRightRadius: 4,
  },
  bubbleAssistant: {
    alignSelf: 'flex-start',
    backgroundColor: theme.surfaceAlt,
    borderBottomLeftRadius: 4,
    borderWidth: 1,
    borderColor: theme.border,
  },
  bubbleText: { color: theme.text, fontSize: 14, lineHeight: 20 },
  intent: {
    color: theme.textDim,
    fontSize: 10,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 1,
    marginBottom: 3,
  },
  composer: {
    flexDirection: 'row',
    gap: 8,
    padding: theme.spacing,
    borderTopWidth: 1,
    borderTopColor: theme.border,
    backgroundColor: theme.surface,
  },
  input: {
    flex: 1,
    backgroundColor: theme.surfaceAlt,
    borderRadius: theme.radius,
    borderWidth: 1,
    borderColor: theme.border,
    color: theme.text,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    maxHeight: 110,
  },
  send: {
    width: 46,
    borderRadius: theme.radius,
    backgroundColor: theme.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  sendDisabled: { opacity: 0.4 },
  sendText: { color: theme.accentText, fontSize: 18, fontWeight: '800' },
});
