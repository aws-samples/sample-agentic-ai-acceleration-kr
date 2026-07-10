// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getRequestConfig } from 'next-intl/server';

export default getRequestConfig(async () => {
  // Currently fixed to Korean. To support locale switching,
  // read from cookies/headers and validate against supported locales.
  const locale = 'ko';

  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
  };
});
