// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { render, screen } from '@testing-library/react';
import { KPICard } from '@/components/common/KPICard';

describe('KPICard', () => {
  it('renders title and value', () => {
    render(<KPICard title="테스트" value={42} icon={<span>icon</span>} />);
    expect(screen.getByText('테스트')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  it('applies CRITICAL alert style', () => {
    const { container } = render(
      <KPICard title="위험" value="100%" icon={<span>icon</span>} alertLevel="CRITICAL" />
    );
    expect(container.firstChild).toHaveClass('border-red-500');
  });

  it('applies WARNING alert style', () => {
    const { container } = render(
      <KPICard title="경고" value="85%" icon={<span>icon</span>} alertLevel="WARNING" />
    );
    expect(container.firstChild).toHaveClass('border-yellow-500');
  });
});
