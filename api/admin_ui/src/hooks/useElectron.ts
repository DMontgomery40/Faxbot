import { useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

// Hook for Electron-specific functionality
export const useElectron = () => {
  const navigate = useNavigate();

  // Check if running in Electron
  const isElectron = typeof window !== 'undefined' && (window as any).electronAPI?.isElectron === true;

  // Handle navigation from Electron menu
  useEffect(() => {
    if (!isElectron) return;

    const handleNavigate = (route: string) => {
      navigate(route);
    };

    // Listen for navigation events from Electron menu
    if ((window as any).electronAPI?.onNavigate) {
      (window as any).electronAPI.onNavigate(handleNavigate);
    }

    // Cleanup
    return () => {
      if ((window as any).electronAPI?.removeNavigateListener) {
        (window as any).electronAPI.removeNavigateListener();
      }
    };
  }, [navigate, isElectron]);

  // File selection using Electron dialog
  const selectFile = useCallback(async (): Promise<{ filePaths: string[]; canceled: boolean } | null> => {
    if (!isElectron || !(window as any).electronAPI?.selectFile) {
      return null;
    }
    return await (window as any).electronAPI.selectFile();
  }, [isElectron]);

  // Show native message box
  const showMessageBox = useCallback(async (options: {
    type?: 'none' | 'info' | 'error' | 'question' | 'warning';
    title?: string;
    message: string;
    detail?: string;
    buttons?: string[];
  }): Promise<{ response: number; checkboxChecked?: boolean } | null> => {
    if (!isElectron || !(window as any).electronAPI?.showMessageBox) {
      return null;
    }
    return await (window as any).electronAPI.showMessageBox(options);
  }, [isElectron]);

  // Get app version
  const getAppVersion = useCallback(async (): Promise<string | null> => {
    if (!isElectron || !(window as any).electronAPI?.getAppVersion) {
      return null;
    }
    return await (window as any).electronAPI.getAppVersion();
  }, [isElectron]);

  // Get platform info
  const getPlatform = useCallback((): string => {
    if (isElectron && (window as any).electronAPI?.platform) {
      return (window as any).electronAPI.platform;
    }
    return 'web';
  }, [isElectron]);

  return {
    isElectron,
    selectFile,
    showMessageBox,
    getAppVersion,
    getPlatform
  };
};

export default useElectron;

