import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  CheckCircle2,
  Link2,
  Loader2,
  Sparkles,
  ShoppingBag,
  Search,
  AlertTriangle,
  LogIn,
  SquarePen,
  Square,
} from "lucide-react";
import ReactMarkdown from "react-markdown";

export const Route = createFileRoute("/")({
  component: FoodPilot,
});

const API_BASE = "http://localhost:8000";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

type ThoughtStep = {
  id: string;
  label: string;
  status: "active" | "done";
  icon: "link" | "search" | "cart" | "spark";
};

type SwiggyStatus = "unknown" | "ok" | "expiring_soon" | "disconnected";

type User = {
  name: string;
  email: string;
  avatar_url?: string;
};

function FoodPilot() {
  const [user, setUser] = useState<User | null | undefined>(undefined); // undefined = loading
  const [conversationId, setConversationId] = useState<string | null>(localStorage.getItem("foodpilot_conv_id") || null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [thoughts, setThoughts] = useState<ThoughtStep[]>([]);
  const [swiggyStatus, setSwiggyStatus] = useState<SwiggyStatus>("unknown");
  const [needsSwiggy, setNeedsSwiggy] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const stopGeneration = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  const scrollToBottom = useCallback((force = false) => {
    setTimeout(() => {
      if (!scrollRef.current) return;
      const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
      // Auto-scroll if forced or if already near the bottom
      if (force || scrollHeight - scrollTop - clientHeight < 350) {
        if (messagesEndRef.current) {
          messagesEndRef.current.scrollIntoView({ behavior: "auto", block: "end" });
        } else {
          scrollRef.current.scrollTo({ top: scrollHeight, behavior: "auto" });
        }
      }
    }, 10);
  }, []);

  const hasMessages = messages.length > 0;

  // Check user session
  useEffect(() => {
    const fetchUser = async () => {
      try {
        const res = await fetch(`${API_BASE}/auth/me`, { credentials: "include" });
        if (res.ok) {
          const data = await res.json();
          setUser(data);
        } else {
          setUser(null);
        }
      } catch {
        setUser(null);
      }
    };
    fetchUser();
  }, []);

  // Swiggy health poll (only if user is logged in)
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const check = async () => {
      try {
        const r = await fetch(`${API_BASE}/auth/swiggy/health`, {
          credentials: "include",
        });
        if (!r.ok) throw new Error();
        const data = (await r.json()) as { status?: string };
        if (cancelled) return;
        if (data.status === "ok") setSwiggyStatus("ok");
        else if (data.status === "expiring_soon") setSwiggyStatus("expiring_soon");
        else setSwiggyStatus("disconnected");
      } catch {
        if (!cancelled) setSwiggyStatus("disconnected");
      }
    };
    check();
    const t = setInterval(check, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [user]);

  // Load conversation history
  useEffect(() => {
    if (!user || swiggyStatus !== "ok" || !conversationId) return;
    const fetchHistory = async () => {
      try {
        const res = await fetch(`${API_BASE}/chat/${conversationId}`, { credentials: "include" });
        if (res.ok) {
          const data = await res.json();
          // API returns { role: 'user', content: '...', id: '...' }
          setMessages(data.messages);
        } else {
          setConversationId(null);
          localStorage.removeItem("foodpilot_conv_id");
        }
      } catch {
        setConversationId(null);
        localStorage.removeItem("foodpilot_conv_id");
      }
    };
    fetchHistory();
  }, [user, swiggyStatus, conversationId]);

  useEffect(() => {
    scrollToBottom(true);
  }, [messages.length, thoughts.length, isStreaming, scrollToBottom]);

  const pushThought = useCallback((step: Omit<ThoughtStep, "id" | "status">) => {
    setThoughts((prev) => {
      const next: ThoughtStep[] = prev.map((s) => ({ ...s, status: "done" }));
      next.push({ ...step, id: crypto.randomUUID(), status: "active" });
      return next;
    });
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || isStreaming) return;
    setWarning(null);
    setNeedsSwiggy(false);

    setMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), role: "user", content: text },
      { id: "temp-loading", role: "assistant", content: "" },
    ]);
    setInput("");
    setIsStreaming(true);
    setThoughts([]);
    pushThought({ label: "Connecting to Swiggy…", icon: "link" });

    try {
      abortControllerRef.current = new AbortController();
      const endpoint = conversationId ? `${API_BASE}/chat/${conversationId}` : `${API_BASE}/chat`;
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        credentials: "include",
        body: JSON.stringify({ message: text }),
        signal: abortControllerRef.current.signal,
      });

      if (res.status === 403 || res.status === 419) {
        setNeedsSwiggy(true);
        setSwiggyStatus("disconnected");
        setMessages((m) =>
          m.map((msg) =>
            msg.id === "temp-loading"
              ? { ...msg, id: crypto.randomUUID(), content: "Your Swiggy session needs to be reconnected to continue." }
              : msg,
          ),
        );
        return;
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

      pushThought({ label: "Searching restaurants…", icon: "search" });

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let firstChunk = true;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const payload = trimmed.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          try {
            const evt = JSON.parse(payload) as {
              type: string;
              text?: string;
              content?: string;
              code?: string;
              label?: string;
              conversation_id?: string;
            };
            if (evt.type === "chunk" && (evt.content || evt.text)) {
              const textToAppend = evt.content || evt.text || "";
              if (firstChunk) {
                pushThought({ label: "Building response…", icon: "spark" });
                firstChunk = false;
              }
              setMessages((prev) => {
                const newMessages = [...prev];
                const last = newMessages[newMessages.length - 1];
                if (last && last.role === "assistant") {
                  newMessages[newMessages.length - 1] = {
                    ...last,
                    id: last.id === "temp-loading" ? crypto.randomUUID() : last.id,
                    content: last.content + textToAppend
                  };
                } else {
                  newMessages.push({
                    id: crypto.randomUUID(),
                    role: "assistant",
                    content: textToAppend
                  });
                }
                return newMessages;
              });
            } else if (evt.type === "warning") {
              if (evt.code === "SWIGGY_TOKEN_EXPIRING_SOON") {
                setSwiggyStatus("expiring_soon");
                setWarning("Your Swiggy session is expiring soon — reconnect to avoid interruptions.");
              } else {
                setWarning(evt.code ?? "Warning");
              }
            } else if (evt.type === "thought" && evt.label) {
              pushThought({ label: evt.label, icon: "search" });
            } else if (evt.type === "cart") {
              pushThought({ label: "Building cart…", icon: "cart" });
            } else if (evt.type === "error" || evt.type === "fallback") {
              setMessages((m) => {
                const newM = [...m];
                const last = newM[newM.length - 1];
                if (last && last.role === "assistant") {
                  newM[newM.length - 1] = {
                    ...last,
                    id: last.id === "temp-loading" ? crypto.randomUUID() : last.id,
                    content: (last.content || "") + (last.content ? "\n\n" : "") + (evt as any).message
                  };
                }
                return newM;
              });
            } else if (evt.type === "done" && evt.conversation_id) {
              if (!conversationId) {
                setConversationId(evt.conversation_id);
                localStorage.setItem("foodpilot_conv_id", evt.conversation_id);
              }
            }
          } catch {
            /* ignore malformed */
          }
        }
      }
      setThoughts((prev) => prev.map((s) => ({ ...s, status: "done" })));
    } catch (err: any) {
      if (err.name === "AbortError") return;
      setMessages((m) => {
        const newM = [...m];
        const last = newM[newM.length - 1];
        if (last && last.role === "assistant") {
          newM[newM.length - 1] = {
            ...last,
            id: last.id === "temp-loading" ? crypto.randomUUID() : last.id,
            content: last.content || "Couldn't reach FoodPilot backend. Make sure the API is running at localhost:8000."
          };
        }
        return newM;
      });
    } finally {
      abortControllerRef.current = null;
      setIsStreaming(false);
    }
  }, [input, isStreaming, pushThought, conversationId]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="flex min-h-screen flex-col text-foreground">
      <Header
        swiggyStatus={swiggyStatus}
        user={user}
      />

      {swiggyStatus === "expiring_soon" && (
        <div className="mx-auto mt-2 flex w-full max-w-6xl items-center gap-3 rounded-xl border border-amber-400/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">
          <AlertTriangle className="h-4 w-4" />
          <span>Your Swiggy session is expiring soon.</span>
          <a
            href={`${API_BASE}/auth/swiggy/connect`}
            className="ml-auto rounded-md bg-amber-400/20 px-3 py-1 text-xs font-semibold text-amber-100 hover:bg-amber-400/30"
          >
            Reconnect
          </a>
        </div>
      )}
      {warning && (
        <div className="mx-auto mt-2 flex w-full max-w-6xl items-center gap-3 rounded-xl border border-swiggy/30 bg-swiggy/10 px-4 py-2 text-sm text-swiggy-glow">
          <Sparkles className="h-4 w-4" />
          <span>{warning}</span>
        </div>
      )}

      {user === undefined || (user && swiggyStatus === "unknown") ? (
        <main className="flex flex-1 items-center justify-center">
          <Loader2 className="h-8 w-8 animate-spin text-swiggy" />
        </main>
      ) : user === null ? (
        <main className="flex flex-1 items-center justify-center px-4 sm:px-6">
          <div className="w-full max-w-md animate-slide-up text-center glass rounded-3xl p-8 shadow-2xl">
            <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center">
              <img
                src="/swiggy-logo.png"
                alt="Swiggy"
                width={80}
                height={80}
                className="h-full w-full object-contain drop-shadow-[0_10px_30px_rgba(252,128,25,0.45)]"
              />
            </div>
            <h1 className="text-3xl font-bold tracking-tight mb-2">
              Welcome to <span className="text-gradient-swiggy">FoodPilot</span>
            </h1>
            <p className="text-sm text-muted-foreground mb-8">
              Sign in to manage your Swiggy orders and talk to your AI food concierge.
            </p>
            <a
              href={`${API_BASE}/auth/google/login`}
              className="flex w-full items-center justify-center gap-3 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-black transition hover:bg-gray-100 shadow-sm"
            >
              <LogIn className="h-5 w-5" />
              Continue with Google
            </a>
          </div>
        </main>
      ) : swiggyStatus !== "ok" ? (
        <main className="flex flex-1 items-center justify-center px-4 sm:px-6">
          <div className="w-full max-w-md animate-slide-up text-center glass rounded-3xl p-8 shadow-2xl border-swiggy/20">
            <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-swiggy/10">
              <Link2 className="h-8 w-8 text-swiggy" />
            </div>
            <h2 className="text-2xl font-bold tracking-tight mb-2">
              Connect your Swiggy
            </h2>
            <p className="text-sm text-muted-foreground mb-8">
              Link your Swiggy account so FoodPilot can fetch your carts, track orders, and find restaurants near you.
            </p>
            <a
              href={`${API_BASE}/auth/swiggy/connect`}
              className="flex w-full items-center justify-center gap-2 rounded-xl gradient-swiggy px-4 py-3 text-sm font-bold text-black shadow-swiggy transition hover:brightness-110"
            >
              <Link2 className="h-5 w-5" />
              Connect Swiggy Account
            </a>
          </div>
        </main>
      ) : !hasMessages ? (
        <main className="flex flex-1 items-center justify-center px-4 sm:px-6">
          <div className="w-full max-w-2xl animate-slide-up text-center">
            <div className="mx-auto mb-6 md:mb-8 flex h-16 w-16 md:h-20 md:w-20 items-center justify-center animate-float">
              <img
                src="/swiggy-logo.png"
                alt="Swiggy"
                width={80}
                height={80}
                className="h-full w-full object-contain drop-shadow-[0_10px_30px_rgba(252,128,25,0.45)]"
              />
            </div>
            <h1 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight">
              What are you <span className="text-gradient-swiggy">craving</span> today?
            </h1>
            <p className="mt-3 text-sm sm:text-base text-muted-foreground">
              From hot Swiggy meals to fresh Instamart essentials, your AI Pilot is ready.
            </p>
            <div className="mt-8">
              <Composer
                value={input}
                onChange={setInput}
                onSend={send}
                onStop={stopGeneration}
                onKeyDown={onKeyDown}
                isStreaming={isStreaming}
                ref={textareaRef}
                large
              />
            </div>
          </div>
        </main>
      ) : (
        <main className="flex w-full flex-1 flex-col pb-6 pt-2">
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 md:px-6 py-6 relative">

            {/* New Chat Button on the far left, under Swiggy logo */}
            <div className="absolute left-4 md:left-6 top-6 z-10 hidden sm:block">
              <button
                onClick={async () => {
                  if (conversationId) {
                    try {
                      await fetch(`${API_BASE}/chat/${conversationId}`, {
                        method: "DELETE",
                        credentials: "include"
                      });
                    } catch (err) {
                      console.error("Failed to delete chat", err);
                    }
                  }
                  setConversationId(null);
                  setMessages([]);
                  localStorage.removeItem("foodpilot_conv_id");
                }}
                className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
              >
                <SquarePen className="h-4 w-4" />
                New chat
              </button>
            </div>

            {/* Centered Chat Messages */}
            <div className="mx-auto w-full max-w-3xl space-y-8">
              {messages.map((m, idx) => (
                <MessageBubble
                  key={m.id}
                  message={m}
                  streaming={isStreaming && idx === messages.length - 1}
                  onContentChange={() => scrollToBottom(false)}
                  onSend={send}
                />
              ))}
              {/* Invisible anchor element to scroll into view */}
              <div ref={messagesEndRef} className="h-4" />
            </div>
          </div>
          <div className="sticky bottom-0 pb-4 pt-3 mx-auto w-full max-w-3xl px-4 md:px-0">
            <Composer
              value={input}
              onChange={setInput}
              onSend={send}
              onStop={stopGeneration}
              onKeyDown={onKeyDown}
              isStreaming={isStreaming}
              ref={textareaRef}
            />
          </div>
        </main>
      )}
    </div>
  );
}

function Header({
  swiggyStatus,
  user
}: {
  swiggyStatus: SwiggyStatus;
  user: User | null | undefined;
}) {
  const [showLogout, setShowLogout] = useState(false);

  const handleLogout = async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" });
      window.location.reload();
    } catch {
      window.location.reload();
    }
  };

  return (
    <>
      <header className="flex w-full items-center justify-between gap-2 px-4 py-3 md:px-6 md:py-4">
        <div className="flex items-center gap-4 min-w-0">
          <a href="/" className="flex items-center gap-2 min-w-0">
            <img
              src="/swiggy-logo.png"
              alt="Swiggy"
              width={36}
              height={36}
              className="h-8 w-8 md:h-9 md:w-9 rounded-lg object-contain shrink-0"
            />
            <span className="flex items-baseline gap-1 min-w-0">
              <span className="text-muted-foreground text-sm md:text-base">×</span>
              <span className="text-[10px] md:text-xs font-medium tracking-wide text-foreground/80 truncate">
                FoodPilot
              </span>
            </span>
          </a>
        </div>

        <div className="flex items-center gap-3">
          {swiggyStatus !== "unknown" && <SwiggyPill status={swiggyStatus} />}
          {user && (
            <div className="flex items-center gap-3 ml-2 border-l border-white/10 pl-3">
              {user.avatar_url && (
                <img src={user.avatar_url} alt={user.name} className="h-8 w-8 rounded-full ml-1" />
              )}
              <button
                onClick={() => setShowLogout(true)}
                className="text-xs font-semibold text-red-400 hover:text-red-300 transition"
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </header>

      {showLogout && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm animate-fade-in">
          <div className="glass flex w-full max-w-[320px] flex-col gap-4 rounded-3xl p-6 shadow-2xl animate-slide-up">
            <h3 className="text-lg font-semibold">Sign Out</h3>
            <p className="text-[15px] text-muted-foreground leading-snug">
              Are you sure you want to log out of FoodPilot?
            </p>
            <div className="flex justify-end gap-2 mt-2">
              <button
                onClick={() => setShowLogout(false)}
                className="rounded-xl px-4 py-2.5 text-sm font-medium text-foreground hover:bg-white/10 transition"
              >
                Cancel
              </button>
              <button
                onClick={handleLogout}
                className="rounded-xl bg-red-500/10 px-4 py-2.5 text-sm font-semibold text-red-400 hover:bg-red-500/20 transition"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function SwiggyPill({ status }: { status: SwiggyStatus }) {
  if (status === "ok") {
    return null; // Redundant since chat only works when connected
  }
  if (status === "expiring_soon") {
    return (
      <a
        href={`${API_BASE}/auth/swiggy/connect`}
        className="glass inline-flex items-center gap-2 rounded-full px-3 py-2 text-xs font-medium text-amber-300 hover:border-amber-400/40"
      >
        <AlertTriangle className="h-3.5 w-3.5" />
        Reconnect Swiggy
      </a>
    );
  }
  return (
    <a
      href={`${API_BASE}/auth/swiggy/connect`}
      className="inline-flex items-center gap-2 rounded-full gradient-swiggy px-4 py-2 text-xs font-semibold text-black shadow-swiggy transition hover:brightness-110"
    >
      <Link2 className="h-3.5 w-3.5" />
      Connect Swiggy
    </a>
  );
}

function MessageBubble({ message, streaming, onContentChange, onSend }: { message: ChatMessage; streaming: boolean; onContentChange?: () => void; onSend?: (text: string) => void }) {
  const isUser = message.role === "user";
  const [displayedContent, setDisplayedContent] = useState(isUser ? message.content : "");

  const processedLengthRef = useRef(0);
  const queueRef = useRef<string[]>([]);

  // 1. Enqueue incoming characters
  useEffect(() => {
    if (isUser) {
      setDisplayedContent(message.content);
      return;
    }
    // Only process new characters we haven't seen yet
    const newChars = message.content.slice(processedLengthRef.current);
    if (newChars.length > 0) {
      queueRef.current.push(...newChars.split(""));
      processedLengthRef.current = message.content.length;
    }
  }, [message.content, isUser]);

  // 2. Drain queue at fixed smooth rate
  useEffect(() => {
    if (isUser) return;

    if (!streaming) {
      // Stream finished (or initialized with a completed message)
      // Flush anything remaining in the queue instantly to prevent clipping
      if (queueRef.current.length > 0) {
        setDisplayedContent((prev) => prev + queueRef.current.join(""));
        queueRef.current = [];
      } else {
        // Fallback catch-up if initialized with completed message
        setDisplayedContent(message.content);
      }
      return;
    }

    const interval = setInterval(() => {
      if (queueRef.current.length > 0) {
        // Pop ~2 chars every 16ms = ~125 chars/sec for smooth ~60fps rendering
        const chunk = queueRef.current.splice(0, 2).join("");
        setDisplayedContent((prev) => prev + chunk);
      }
    }, 16);

    return () => clearInterval(interval);
  }, [streaming, isUser]);

  const contentToRender = isUser ? message.content : displayedContent;

  const buttonRegex = /\[BUTTON:\s*(.*?)\]/g;
  const buttons: string[] = [];
  let cleanContent = contentToRender;

  if (!isUser) {
    let match;
    while ((match = buttonRegex.exec(contentToRender)) !== null) {
      buttons.push(match[1].trim());
    }
    cleanContent = contentToRender.replace(buttonRegex, "").trim();
  }

  // Trigger scroll *after* content renders to DOM
  useEffect(() => {
    if (streaming && onContentChange) {
      onContentChange();
    }
  }, [contentToRender, streaming]);

  if (isUser) {
    return (
      <div className="flex animate-slide-up justify-end">
        <div className="max-w-[75%] rounded-3xl bg-zinc-700/80 px-5 py-2.5 text-[15px] text-white">
          {contentToRender}
        </div>
      </div>
    );
  }

  // Assistant message (ChatGPT style: no bubble, full width, icon on left)
  return (
    <div className="flex animate-slide-up justify-start gap-4 md:gap-5 w-full">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#FC8019] shadow-sm mt-1">
        <img src="/swiggy-logo.png" alt="FoodPilot" className="h-5 w-5 object-contain brightness-0 invert" />
      </div>
      <div className="flex-1 overflow-hidden">
        {contentToRender ? (
          <ReactMarkdown
            components={{
              p: ({ node, ...props }) => <p className="mb-4 last:mb-0 text-foreground/95 leading-[1.7] text-[15px] whitespace-pre-wrap" {...props} />,
              strong: ({ node, ...props }) => <strong className="font-semibold text-foreground" {...props} />,
              ol: ({ node, ...props }) => <ol className="list-decimal pl-8 mb-4 space-y-2" {...props} />,
              ul: ({ node, ...props }) => <ul className="list-disc pl-8 mb-4 space-y-2" {...props} />,
              li: ({ node, ...props }) => <li className="text-foreground/95 leading-[1.7] text-[15px]" {...props} />,
              img: ({ node, ...props }) => (
                <img
                  className="rounded-xl mt-2 mb-3 max-h-[160px] object-cover shadow-sm border border-white/5 bg-white/5"
                  onError={(e) => { e.currentTarget.style.display = 'none'; }}
                  {...props}
                />
              ),
            }}
          >
            {cleanContent}
          </ReactMarkdown>
        ) : (
          streaming && <div className="mt-1.5"><StreamingDots /></div>
        )}

        {buttons.length > 0 && !streaming && (
          <div className="flex flex-wrap gap-2 mt-4 animate-slide-up">
            {buttons.map((btn, i) => (
              <button
                key={i}
                onClick={() => onSend?.(btn)}
                className="px-4 py-2 text-[13px] font-medium tracking-wide rounded-full bg-white/5 border border-white/10 hover:bg-white/10 hover:border-white/20 transition-all text-white/90"
              >
                {btn}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StreamingDots() {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="h-1.5 w-1.5 animate-pulse-glow rounded-full bg-swiggy" />
      <span
        className="h-1.5 w-1.5 animate-pulse-glow rounded-full bg-swiggy"
        style={{ animationDelay: "150ms" }}
      />
      <span
        className="h-1.5 w-1.5 animate-pulse-glow rounded-full bg-swiggy"
        style={{ animationDelay: "300ms" }}
      />
    </span>
  );
}

const iconMap = {
  link: Link2,
  search: Search,
  cart: ShoppingBag,
  spark: Sparkles,
};

function ThoughtConsole({ thoughts, active }: { thoughts: ThoughtStep[]; active: boolean }) {
  return (
    <div className="glass sticky top-6 flex h-[calc(100vh-8rem)] flex-col overflow-hidden rounded-3xl p-5">
      <div className="mb-4 flex items-center gap-2">
        <div className="relative flex h-8 w-8 items-center justify-center rounded-lg gradient-swiggy">
          <Sparkles className="h-4 w-4 text-black" />
          {active && (
            <span className="absolute inset-0 animate-pulse-glow rounded-lg bg-swiggy/40" />
          )}
        </div>
        <div>
          <div className="text-sm font-semibold">Thought Console</div>
          <div className="text-xs text-muted-foreground">
            {active ? "Thinking…" : "Idle"}
          </div>
        </div>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto pr-1">
        {thoughts.length === 0 && (
          <div className="rounded-xl border border-dashed border-white/10 p-6 text-center text-xs text-muted-foreground">
            Live reasoning from FoodPilot will appear here.
          </div>
        )}
        {thoughts.map((t) => {
          const Icon = iconMap[t.icon];
          return (
            <div
              key={t.id}
              className={`flex items-center gap-3 rounded-xl border px-3 py-3 text-sm transition ${t.status === "active"
                ? "border-swiggy/40 bg-swiggy/10 text-foreground"
                : "border-white/5 bg-white/[0.02] text-muted-foreground"
                }`}
            >
              <div
                className={`flex h-8 w-8 items-center justify-center rounded-lg ${t.status === "active" ? "gradient-swiggy" : "bg-white/5"
                  }`}
              >
                {t.status === "active" ? (
                  <Loader2 className="h-4 w-4 animate-spin text-black" />
                ) : (
                  <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                )}
              </div>
              <div className="flex-1">{t.label}</div>
              <Icon className="h-4 w-4 opacity-60" />
            </div>
          );
        })}
      </div>

      <div className="mt-4 rounded-xl border border-white/5 bg-black/30 p-3 text-[11px] text-muted-foreground">
        <div className="flex items-center justify-between">
          <span>API</span>
          <span className="font-mono">localhost:8000</span>
        </div>
      </div>
    </div>
  );
}

type ComposerProps = {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  isStreaming: boolean;
  large?: boolean;
};

const Composer = (() => {
  const C = (
    props: ComposerProps & { forwardedRef?: React.Ref<HTMLTextAreaElement> },
  ) => {
    const { value, onChange, onSend, onStop, onKeyDown, isStreaming, large, forwardedRef } = props;
    return (
      <div
        className={`glass group relative flex items-center gap-2 rounded-[28px] p-1.5 transition focus-within:border-swiggy/50 focus-within:shadow-swiggy w-full`}
      >
        <textarea
          ref={forwardedRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={(e) => {
            const target = e.target as HTMLTextAreaElement;
            target.style.height = large ? '52px' : '40px';
            target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
          }}
          rows={1}
          style={{ height: large ? '52px' : '40px', maxHeight: '200px' }}
          placeholder="Ask FoodPilot to order, plan, or restock…"
          className={`flex-1 resize-none bg-transparent pl-4 pr-2 py-[9px] text-[15px] leading-snug text-foreground placeholder:text-muted-foreground/70 focus:outline-none overflow-y-auto ${large ? "text-lg py-[13px]" : ""
            }`}
        />
        <button
          onClick={isStreaming ? onStop : onSend}
          disabled={!value.trim() && !isStreaming}
          className="mr-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full gradient-swiggy text-black shadow-swiggy transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
          aria-label={isStreaming ? "Stop" : "Send"}
        >
          {isStreaming ? (
            <Square className="h-3.5 w-3.5 fill-black" strokeWidth={3} />
          ) : (
            <ArrowUp className="h-4 w-4 block" strokeWidth={2.5} />
          )}
        </button>
      </div>
    );
  };
  return Object.assign(
    (props: ComposerProps & { ref?: React.Ref<HTMLTextAreaElement> }) => {
      const { ref, ...rest } = props;
      return <C {...rest} forwardedRef={ref} />;
    },
  );
})();
