import React from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { AppointmentModule } from "./AppointmentModule";
createRoot(document.getElementById("root")!).render(<React.StrictMode><AppointmentModule /></React.StrictMode>);
