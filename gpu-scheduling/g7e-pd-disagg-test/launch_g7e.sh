#!/bin/bash
# 启动 1 或 2 台 g7e.48xlarge（cluster placement group + 4 EFA）。默认 DRY_RUN=1，只打印命令不执行。
# 用法：DRY_RUN=0 NODES=2 KEY_NAME=k SUBNET_ID=subnet-x SG_ID=sg-x ./launch_g7e.sh
set -euo pipefail
REGION=${REGION:-us-east-1}
NODES=${NODES:-1}
ITYPE=${ITYPE:-g7e.48xlarge}
PG=${PG:-pd-test-pg}
DRY_RUN=${DRY_RUN:-1}
: "${KEY_NAME:?需要 KEY_NAME}" "${SUBNET_ID:?需要 SUBNET_ID}" "${SG_ID:?需要 SG_ID（组内全放通）}"
AMI=${AMI:-$(aws ssm get-parameter --region $REGION \
  --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query Parameter.Value --output text)}
NUM_EFA=$(aws ec2 describe-instance-types --region $REGION --instance-types $ITYPE \
  --query 'InstanceTypes[0].NetworkInfo.EfaInfo.MaximumEfaInterfaces' --output text)
NI=""
for i in $(seq 0 $((NUM_EFA-1))); do
  [ $i -eq 0 ] && DI=0 || DI=1
  NI+="NetworkCardIndex=$i,DeviceIndex=$DI,InterfaceType=efa,SubnetId=$SUBNET_ID,Groups=$SG_ID "
done
PRICE_NOTE="按需约 \$33/h/台（us-east-1, 2026-11）"
CMD1="aws ec2 create-placement-group --region $REGION --group-name $PG --strategy cluster"
CMD2="aws ec2 run-instances --region $REGION --image-id $AMI --instance-type $ITYPE --count $NODES \
 --key-name $KEY_NAME --placement GroupName=$PG --network-interfaces $NI \
 --block-device-mappings DeviceName=/dev/sda1,Ebs={VolumeSize=500,VolumeType=gp3} \
 --tag-specifications ResourceType=instance,Tags=[{Key=Name,Value=pd-test}]"
echo "AMI=$AMI  EFA=$NUM_EFA  NODES=$NODES  $PRICE_NOTE"
echo "$CMD1"; echo "$CMD2"
if [ "$DRY_RUN" = "1" ]; then echo ">>> DRY_RUN=1，未执行。确认后用 DRY_RUN=0 重跑。"; exit 0; fi
read -p "确认启动 $NODES 台 $ITYPE？(yes/no) " a; [ "$a" = yes ] || exit 1
$CMD1 2>/dev/null || true
eval $CMD2 --query 'Instances[].InstanceId' --output text
echo "注意：多网卡实例不会自动分配公网 IP，需要绑定 EIP 到 eth0 或通过跳板机访问。用完记得 terminate。"
