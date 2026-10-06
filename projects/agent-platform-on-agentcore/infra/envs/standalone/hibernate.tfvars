# Hibernation override: pause the environment without destroying it.
# Removes NAT gateway and EIP (cost savings), running 0 tasks.
# Apply with: terraform apply -var-file=hibernate.tfvars
hibernate     = true
desired_count = 0
