import React, { useState, useCallback, useEffect } from "react";
import { useDropzone } from "react-dropzone";
import {
  FileText,
  Send,
  Upload,
  Bot,
  Loader,
  Trash2,
  Files,
  LogIn,
  LogOut,
  Menu,
} from "lucide-react";
import { auth, signIn, signUp, logOut, signInWithGoogle } from "./firebase";
import { useFingerprint } from "./hooks/useFingerprint";
import { AuthModal } from "./components/AuthModal";

function App() {
  const [file, setFile] = useState(null);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [user, setUser] = useState(null);
  const [apiKey, setApiKey] = useState(null);
  const [authModal, setAuthModal] = useState({ isOpen: false, mode: "signin" });
  const fingerprint = useFingerprint();

  const BACKEND_URL =
    import.meta.env.VITE_BACKEND_URL || "http://127.0.0.1:8000";

  const handleGoogleSignIn = async () => {
    try {
      await signInWithGoogle();
      // The auth state change will trigger fetchApiKey automatically
    } catch (error) {
      console.error("Google sign-in error:", error);
      throw error;
    }
  };

  useEffect(() => {
    const unsubscribe = auth.onAuthStateChanged((currentUser) => {
      setUser(currentUser);
      if (currentUser) {
        fetchApiKey(currentUser); // Pass the user directly
      }
    });

    return () => unsubscribe();
  }, []);

  const fetchApiKey = async (currentUser) => {
    try {
      const headers = {
        "Content-Type": "application/json",
      };

      // Use the user passed as parameter
      if (currentUser) {
        const idToken = await currentUser.getIdToken();
        headers["Authorization"] = `Bearer ${idToken}`;
      }

      if (fingerprint) {
        headers["X-Device-Fingerprint"] = fingerprint;
      }

      const response = await fetch(`${BACKEND_URL}/create-api-key`, {
        method: "POST",
        headers: headers,
      });

      if (!response.ok) {
        const errorText = await response.text();
        console.error("API key error:", response.status, errorText);
        throw new Error(
          `Failed to get API key: ${response.status} ${errorText}`
        );
      }

      const data = await response.json();
      console.log("API key received:", data.api_key);
      setApiKey(data.api_key);
    } catch (error) {
      console.error("Error fetching API key:", error);
    }
  };

  const handleAuth = async (email, password) => {
    try {
      if (authModal.mode === "signup") {
        await signUp(email, password);
      } else {
        await signIn(email, password);
      }
    } catch (error) {
      if (
        error.message.includes("auth/email-already-in-use") &&
        authModal.mode === "signup"
      ) {
        // Show a message or switch to Sign In mode
        setAuthModal({ isOpen: true, mode: "signin" });
        alert("This email is already registered. Please sign in instead.");
        return;
      }

      alert(`Authentication error: ${error.message}`);
    }
  };

  const handleLogout = async () => {
    try {
      await logOut();
      setApiKey(null);
      setMessages([]);
      setFile(null);
    } catch (error) {
      console.error("Logout error:", error);
    }
  };

  const onDrop = useCallback(
    async (acceptedFiles) => {
      if (!user) {
        setMessages((prev) => [
          ...prev,
          {
            type: "system",
            content: "Please log in to upload files.",
          },
        ]);
        return;
      }

      const pdfFile = acceptedFiles[0];
      if (pdfFile?.type === "application/pdf") {
        setFile(pdfFile);
        setLoading(true);

        try {
          const formData = new FormData();
          formData.append("file", pdfFile);

          const response = await fetch(`${BACKEND_URL}/upload`, {
            method: "POST",
            headers: {
              "X-API-Key": apiKey,
              "X-Device-Fingerprint": fingerprint,
            },
            body: formData,
          });

          if (!response.ok) throw new Error("Failed to process PDF");

          setMessages((prev) => [
            ...prev,
            {
              type: "system",
              content: `Uploaded and processed: ${pdfFile.name}`,
            },
          ]);
        } catch (error) {
          setMessages((prev) => [
            ...prev,
            {
              type: "system",
              content: `Error processing PDF: ${error.message}`,
            },
          ]);
          setFile(null);
        } finally {
          setLoading(false);
        }
      }
    },
    [user, apiKey, fingerprint]
  );

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!question.trim() || !file || !user) return;

    setMessages((prev) => [
      ...prev,
      {
        type: "user",
        content: question,
      },
    ]);

    setLoading(true);
    try {
      const response = await fetch(`${BACKEND_URL}/ask`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": apiKey,
          "X-Device-Fingerprint": fingerprint,
        },
        body: JSON.stringify({ question }),
      });

      if (!response.ok) throw new Error("Failed to get answer");

      const { answer, context } = await response.json();
      setMessages((prev) => [
        ...prev,
        {
          type: "assistant",
          content: answer,
        },
      ]);
    } catch (error) {
      setMessages((prev) => [
        ...prev,
        {
          type: "system",
          content: `Error: ${error.message}`,
        },
      ]);
    } finally {
      setLoading(false);
      setQuestion("");
    }
  };

  const removeFile = () => {
    setFile(null);
    setMessages([]);
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "application/pdf": [".pdf"] },
    multiple: false,
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-900 to-gray-800 text-gray-100 flex relative">
      {/* Mobile Menu Button */}
      <button
        onClick={() => setSidebarOpen(!sidebarOpen)}
        className="md:hidden fixed top-4 left-4 z-50 p-2 bg-gray-800 rounded-lg shadow-lg hover:bg-gray-700 transition-colors"
      >
        <Menu className="h-6 w-6" />
      </button>

      {/* Overlay for mobile sidebar */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black bg-opacity-50 z-40 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <div
        className={`md:relative fixed md:static top-0 left-0 w-[280px] bg-gray-800 border-r border-gray-700 z-50 transition-transform duration-300 transform ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        } md:translate-x-0 h-full md:h-auto`}
      >
        <div className="p-4 h-full overflow-y-auto">
          <div className="flex items-center justify-between mb-8">
            <div className="flex items-center gap-2">
              <Files className="h-6 w-6 text-blue-400" />
              <h2 className="text-xl font-semibold">Documents</h2>
            </div>
            {user ? (
              <button
                onClick={handleLogout}
                className="text-gray-400 hover:text-red-400 transition-colors"
                title="Sign Out"
              >
                <LogOut className="h-5 w-5" />
              </button>
            ) : (
              <div className="flex gap-2">
                <button
                  onClick={() => setAuthModal({ isOpen: true, mode: "signin" })}
                  className="text-gray-400 hover:text-green-400 transition-colors"
                  title="Sign In"
                >
                  <LogIn className="h-5 w-5" />
                </button>
                <button
                  onClick={() => setAuthModal({ isOpen: true, mode: "signup" })}
                  className="text-sm text-gray-400 hover:text-blue-400 transition-colors"
                >
                  Sign Up
                </button>
              </div>
            )}
          </div>

          {user && (
            <div className="mb-4 text-sm text-gray-400">
              Signed in as: {user.email}
            </div>
          )}

          {file ? (
            <div className="bg-gray-700 rounded-lg p-4 mb-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <FileText className="text-blue-400 flex-shrink-0" />
                  <span className="font-medium truncate">{file.name}</span>
                </div>
                <button
                  onClick={removeFile}
                  className="text-gray-400 hover:text-red-400 transition-colors flex-shrink-0"
                >
                  <Trash2 className="h-5 w-5" />
                </button>
              </div>
              <div className="mt-2 text-sm text-gray-400">
                {(file.size / 1024 / 1024).toFixed(2)} MB
              </div>
            </div>
          ) : (
            <div className="text-gray-400 text-sm">
              No documents uploaded yet
            </div>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col min-h-screen">
        <header className="text-center py-6 md:py-8 px-4">
          <h1 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent mb-2">
            PDF Q&A Assistant
          </h1>
          <p className="text-gray-400 text-sm md:text-base">
            Upload a PDF and ask questions about its content
          </p>
        </header>

        <div className="flex-1 max-w-4xl mx-auto w-full px-4 pb-4 flex flex-col">
          {!file && (
            <div
              {...getRootProps()}
              className={`border-2 border-dashed rounded-lg p-6 md:p-8 text-center cursor-pointer transition-all mb-4
                ${
                  isDragActive
                    ? "border-blue-400 bg-gray-800/50"
                    : "border-gray-600 hover:border-gray-500 hover:bg-gray-800/30"
                }`}
            >
              <input {...getInputProps()} />
              <Upload className="mx-auto h-10 w-10 md:h-12 md:w-12 text-blue-400 mb-4" />
              <p className="text-gray-400 text-sm md:text-base">
                {user
                  ? "Drag & drop a PDF file here, or tap to select one"
                  : "Please log in to upload files"}
              </p>
            </div>
          )}

          <div className="flex-1 bg-gray-800 rounded-lg shadow-xl border border-gray-700 flex flex-col">
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {messages.map((message, index) => (
                <div
                  key={index}
                  className={`flex items-start gap-3 animate-fade-in ${
                    message.type === "user" ? "justify-end" : ""
                  }`}
                >
                  {message.type === "assistant" && (
                    <Bot className="h-6 w-6 text-blue-400 flex-shrink-0" />
                  )}
                  <div
                    className={`rounded-lg p-3 max-w-[85%] md:max-w-[75%] break-words ${
                      message.type === "user"
                        ? "bg-blue-500 text-white"
                        : message.type === "system"
                        ? "bg-gray-700 text-gray-300"
                        : "bg-gray-700 text-gray-200"
                    }`}
                  >
                    {message.content}
                  </div>
                </div>
              ))}
              {loading && (
                <div className="flex items-center gap-2 text-gray-400">
                  <Loader className="h-5 w-5 animate-spin" />
                  <span>Processing...</span>
                </div>
              )}
            </div>

            <form onSubmit={handleSubmit} className="p-4 flex gap-2">
              <input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder={
                  user
                    ? "Ask a question about the PDF..."
                    : "Please log in to ask questions"
                }
                className="flex-1 rounded-lg bg-gray-700 border border-gray-600 px-4 py-3 text-gray-100 placeholder-gray-400 focus:outline-none focus:border-blue-400 text-sm md:text-base"
                disabled={!file || loading || !user}
              />
              <button
                type="submit"
                disabled={!file || !question.trim() || loading || !user}
                className="bg-blue-500 text-white rounded-lg px-4 py-2 hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex-shrink-0"
              >
                <Send className="h-5 w-5 md:h-6 md:w-6" />
              </button>
            </form>
          </div>
        </div>
      </div>

      <AuthModal
        isOpen={authModal.isOpen}
        mode={authModal.mode}
        onClose={() => setAuthModal({ ...authModal, isOpen: false })}
        onSubmit={handleAuth}
        onGoogleSignIn={handleGoogleSignIn}
        switchMode={(newMode) => setAuthModal({ isOpen: true, mode: newMode })}
      />
    </div>
  );
}

export default App;
