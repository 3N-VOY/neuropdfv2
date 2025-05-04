import { useState, useEffect } from 'react';
import * as FingerprintJS from '@fingerprintjs/fingerprintjs';

export function useFingerprint() {
  const [fingerprint, setFingerprint] = useState(null);

  useEffect(() => {
    const getFingerprint = async () => {
      try {
        const fp = await FingerprintJS.load();
        const result = await fp.get();
        setFingerprint(result.visitorId);
      } catch (error) {
        console.error('Error generating fingerprint:', error);
      }
    };

    getFingerprint();
  }, []);

  return fingerprint;
}