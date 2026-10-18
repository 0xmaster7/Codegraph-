import React from "react";
import ReactDOM from "react-dom/client";
import App from "./ui/App";
import AccessGate from "./ui/AccessGate";
import "./ui/styles.css";
import "./ui/productShell.css";
import "./ui/redesign.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><AccessGate><App /></AccessGate></React.StrictMode>,
);
