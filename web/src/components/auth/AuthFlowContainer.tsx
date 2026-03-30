"use client";

import Link from "next/link";
import { motion } from "motion/react";
import { OnyxIcon } from "../icons/icons";

export default function AuthFlowContainer({
  children,
  authState,
  footerContent,
}: {
  children: React.ReactNode;
  authState?: "signup" | "login" | "join";
  footerContent?: React.ReactNode;
}) {
  return (
    <div className="p-4 flex flex-col items-center justify-center min-h-screen bg-background">
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: "easeOut" }}
        className="w-full max-w-md flex items-start flex-col bg-background-tint-00 rounded-16 shadow-lg shadow-02 p-6"
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.4, delay: 0.2, ease: "easeOut" }}
        >
          <OnyxIcon size={56} className="text-theme-primary-05" />
        </motion.div>
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4, delay: 0.3 }}
          className="w-full mt-3"
        >
          {children}
        </motion.div>
      </motion.div>
      {authState === "login" && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4, delay: 0.5 }}
          className="text-sm mt-6 text-center w-full text-text-03 mainUiBody mx-auto"
        >
          {footerContent ?? (
            <>
              New to NaArNi Gyan?{" "}
              <Link
                href="/auth/signup"
                className="text-text-05 mainUiAction underline transition-colors duration-200"
              >
                Create an Account
              </Link>
            </>
          )}
        </motion.div>
      )}
      {authState === "signup" && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4, delay: 0.5 }}
          className="text-sm mt-6 text-center w-full text-text-03 mainUiBody mx-auto"
        >
          Already have an account?{" "}
          <Link
            href="/auth/login?autoRedirectToSignup=false"
            className="text-text-05 mainUiAction underline transition-colors duration-200"
          >
            Sign In
          </Link>
        </motion.div>
      )}
    </div>
  );
}
