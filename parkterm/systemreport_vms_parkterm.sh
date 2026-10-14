#! /bin/sh
echo "SystemResport Start"

if [ "$1" != "" ]
then
	GETLOG_PATH=$1;
    echo "Get SERVICE AND SDK LOG PATH $GETLOG_PATH"
else
    GETLOG_PATH=/var/log/
fi

if [ -f "/tmp/diagnosis_progress_info.txt" ]
then
    cat /dev/null > /tmp/diagnosis_progress_info.txt
	echo "0" > /tmp/diagnosis_progress_info.txt
else
    touch /tmp/diagnosis_progress_info.txt
	cat /dev/null > /tmp/diagnosis_progress_info.txt
	echo "0" > /tmp/diagnosis_progress_info.txt
fi

#---------- define -------
INFO_DIR=/var/coredump/systemreport
COMMON_LOG=$INFO_DIR/systemreport.txt
MT_FILE=./tmp.mt
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT_FILE="/var/coredump/systemreport_${TIMESTAMP}.tgz"
DCCARD_DIR=/var/coredump/DcCardInfo
SMARTINFO_DIR=$INFO_DIR/SmartInfo

SOURCE_PATH=/var/logbackup/
BKCORE_LOG_FOLDER=corelog
BKCORE_MEDIA_FOLDER=coremedia
BKSDKADPT_FOLDRE=sdkadpt
BKNETDEVSDK_FOLDER=netdevsdk
BKCLOUDSDK_FOLDER=cloudsdk
BKTMP_PATH=$INFO_DIR/logbcakup
MAXSIZE=307200
DATABASE_LOG_PATH=/var/log/pg_log/
EXPORTBACKUP=/logbackup/

#---------- function -------
#描述：因诊断信息日志越来越大，web策略一个进度间调用30次，一次间隔2s之后超时处理
#功能：防止web处理时超时处理，而底层打包未完成
#用法：在日志输入文件执行时间在10s外，增加进度，写入文件
exportbackuplog()
{
	filefolder=$1
	if [ ! -d $BKTMP_PATH/$filefolder ]
	then 
		mkdir -p $BKTMP_PATH/$filefolder
	fi
	if [ -d $SOURCE_PATH$filefolder ]
	then
		str=`ls -lt $SOURCE_PATH$filefolder | awk '{print $9}'`
		nowtimetmp=`date +%F`
		nowtime=`date +%s -d$nowtimetmp`
		for name in $str
		do
			if [ $name != '' ]
			then
				time=${name:0-23:10}
				filetime=`date +%s -d$time`
				spantime=$(($nowtime - $filetime))
				echo $spantime
				if [ $spantime -ge 0 -a $spantime -le 86400 ]
				then
					cp $SOURCE_PATH$filefolder/$name $BKTMP_PATH/$filefolder -rf 1>/dev/null 2>&1
					rm -rf $SOURCE_PATH$filefolder/$name
				fi
			fi
		done
	fi
}

#### 删除数据库日志，只保留最新的两个
delete_pg_log()
{
	if [ -d $DATABASE_LOG_PATH ]
	then
		filenum=`ls -1 $DATABASE_LOG_PATH | wc -l`
		filename=`ls -rt $DATABASE_LOG_PATH`
	
		for delfilename in $filename
		do
			if [ $filenum -gt 2 ]
			then
				rm  $DATABASE_LOG_PATH$delfilename
				filenum=`ls -1 $DATABASE_LOG_PATH | wc -l`
			else
				break
			fi
		done
	fi
}

#### 删除最早备份日志文件
deloldfile()
{
	filefolder=$1
	strfilename=`ls -lrt $BKTMP_PATH/$filefolder | awk '{print $9}'`
	for name in $strfilename
	do
		kfoldersize=`du -s  -k $BKTMP_PATH |awk '{print $1}'`
		if [ $kfoldersize -ge $MAXSIZE ]
		then
			deletefilename=$BKTMP_PATH/$filefolder/$name
			rm $deletefilename
		else
			break
		fi
	done
}

#检测四种类型日志的总体大小，使其不超过500M
checklogspace()
{
	deloldfile $BKCLOUDSDK_FOLDER
	deloldfile $BKNETDEVSDK_FOLDER
	deloldfile $BKSDKADPT_FOLDRE
	deloldfile $BKCORE_MEDIA_FOLDER
	deloldfile $BKCORE_LOG_FOLDER
}

progress_info()
{
    echo "$1" > /tmp/diagnosis_progress_info.txt
}

exec_cmd()
{
	echo "$1" >> $COMMON_LOG
	`$1 >> $COMMON_LOG 2>>$COMMON_LOG`
	echo >> $COMMON_LOG
	echo "==================" >> $COMMON_LOG
}

exec_not_show_cmd()
{
	`$1 1>> /dev/null 2>>/dev/null`
}

#---------- For Intel ----------
if [ -d /dev/dri ] ; then
	IS_INTEL_FLAG=1
else
	IS_INTEL_FLAG=0
fi

#---------- main -------
if [ ! -f $COMMON_LOG ]; then
	mkdir -p $INFO_DIR
	touch $COMMON_LOG
fi
#--------------------------
echo "exportbackuplog..."
#减少拷贝对cache的消耗，打包备份的全部日志。
#sh-4.2# du -sh /var/logbackup/*
#4.0K    /var/logbackup/0
#4.0K    /var/logbackup/cloudsdk
#200M    /var/logbackup/corelog
#4.0K    /var/logbackup/coremedia
#4.0K    /var/logbackup/HistoryDiagInfoList.txt
#16K     /var/logbackup/lost+found
#200M    /var/logbackup/netdevsdk
#33M     /var/logbackup/sdkadpt
#104K    /var/logbackup/VmsClientlog
#
echo "exportbackuplog core..."
exportbackuplog $BKCORE_LOG_FOLDER
echo "exportbackuplog sdkadpt..."
exportbackuplog $BKSDKADPT_FOLDRE
echo "exportbackuplog sdknet..."
exportbackuplog $BKNETDEVSDK_FOLDER
#echo "exportbackuplog media..."
#exportbackuplog $BKCORE_MEDIA_FOLDER
#echo "exportbackuplog system..."
#exportbackuplog $BKSYSTEMREPORT_FOLDER
#echo "exportbackuplog cloud..."
#exportbackuplog $BKCLOUDSDK_FOLDER

checklogspace
#---------- dccard -------

if [ "$IS_INTEL_FLAG" -eq 1 ]; then
	for slotId in 1 2
	do
		  for portId in 0 1 2
		  do
			exec_not_show_cmd "drv_vtty $slotId $portId -c echo"
			exec_not_show_cmd "drv_vtty $slotId $portId -c dccardinforeport.sh"
		  done
	done
fi

#smartcard
for slotId in 1 2 3 4
do
	  for portId in 0 1 2 3
	  do
		exec_not_show_cmd "drv_vtty $slotId $portId -c echo"
		exec_not_show_cmd "drv_vtty $slotId $portId -c smartreport.sh"
	  done
done

exec_cmd "date"

#---------- collect software version  -------
exec_cmd "update -v"

#---------- collect system info  -------
exec_cmd "uptime"
exec_cmd "cat /proc/loadavg"
exec_cmd "cat /proc/nvram"
exec_cmd "cat /proc/cmdline"
exec_cmd "cat /proc/cpuinfo"
exec_cmd "mt display eth_info all"
exec_cmd "lsmod"

# IVSD29975 当NVR和NAS网络不通时，导致诊断日志导出失败，将df -h命令后台执行
df -h > /var/log/df_msg.log &
progress_info "10"
#---------- collect network info -------
exec_cmd "ifconfig -a"
#exec_cmd "ethtool eth0"
exec_cmd "netstat -an"
exec_cmd "netstat -an"
exec_cmd "route -n"

progress_info "15"

exec_cmd "ip rule show"
exec_cmd "ip route show"
exec_cmd "cat /proc/net/tcp"
exec_cmd "cat /proc/net/udp"
exec_cmd "iptables -t filter -L -n -v"
exec_cmd "iptables -t raw -L -n -v"
exec_cmd "cat /proc/sys/net/netfilter/nf_conntrack_max"
exec_cmd "cat /proc/sys/net/netfilter/nf_conntrack_count"
#----------- collect eeprom info --------------
exec_cmd "cat /sys/bus/i2c/devices/3-0050/eeprom"
#----------- collect emmc/ssd info --------------
if [ -f /proc/mmc/life_monitoring ] ; then
	exec_cmd "cat /proc/mmc/life_monitoring"
else
	exec_cmd "iSMART_64 -d /dev/sda"
fi

progress_info "20"

#---------- collect ps info ------------
exec_cmd "ps"

#---------- collect syslog info --------
exec_cmd "ls -al /var/log"
dmesg > $INFO_DIR/dmesg

#---------- collect sysconfig ----------
progress_info "25"

#---------- collect disks info ---------
exec_cmd "cat /proc/diskstats"
exec_cmd "cat /proc/disk/disk"
exec_cmd "cat /proc/disk/loglevel"
exec_cmd "cat /proc/disk/simulate"
exec_cmd "cat /proc/disk/smart"
exec_cmd "cat /proc/disk/usb"
exec_cmd "cat /proc/filesystems"
exec_cmd "cat /proc/disk/disk_name"
exec_cmd "fdisk -l"
exec_cmd "cat /proc/partitions"
exec_cmd "cat /proc/scsi/scsi"
exec_cmd "cat /proc/mounts"
#exec_cmd "mpstat"

progress_info "30"

#---------- collect memory info --------
exec_cmd "cat /proc/meminfo"
exec_cmd "cat /proc/slabinfo"
exec_cmd "cat /proc/zoneinfo"
exec_cmd "cat /proc/vmstat"
exec_cmd "cat /proc/stat"
exec_cmd "cat /proc/buddyinfo"
exec_cmd "cat /proc/vmallocinfo"

progress_info "35"

#---------- collect driver info --------
#---------- For Hi35xx ----------
if [ -d /proc/umap ] ; then
	IS_HI35XX_FLAG=1
	if [ -f /proc/umap/hdmi0_sink ]; then
		IS_HI3536_FLAG=1
	else
		IS_HI3536_FLAG=0
	fi
	exec_cmd "cat /proc/umap/*"
	exec_cmd "cat /proc/media-mem"
	if [ "$IS_HI3536_FLAG" -eq 1 ]; then
		：
		# ------------- to get slave cpu media-mem
	fi
else
	IS_HI35XX_FLAG=0
	IS_HI3536_FLAG=0
fi
#---------- For Hi3536 Slave CPU ----------

#---------- For Hi3798M  ----------
if [ -d /proc/msp ] ; then
	exec_cmd "cat /proc/msp/*"
	IS_HI3798M_FLAG=1
else
	IS_HI3798M_FLAG=0
fi

progress_info "40"
if [ "$IS_INTEL_FLAG" -eq 1 ]; then
    exec_cmd "lspci -v"
    exec_cmd "lspci -t"
fi

#---------- collect mt info --------
exec_cmd "mt display SD strm"
exec_cmd "mt display SD nc"
exec_cmd "mt display SD all"
exec_cmd "mt display VOD all"
exec_cmd "mt display VOD table"
exec_cmd "mt display conn all"

progress_info "45"
exec_cmd "mt Mod_media display media io"
exec_cmd "mt Mod_media display media si"
exec_cmd "mt Mod_media display streamstrategy task"
exec_cmd "mt Mod_media display streamstrategy default"

progress_info "50"

exec_cmd "mt Mod_plan display plan all"
exec_cmd "mt Mod_plan display template all"
exec_cmd "mt Mod_stor display Stor CB"

progress_info "55"

#ubs
EXECMD="BroadcastSI BroadcastGroup  resource TalkCB"
for x in $EXECMD
do
	exec_cmd "mt Mod_media display Audio $x"
done

progress_info "60"

#RS
exec_cmd "mt Mod_rs display live"

#RC
exec_cmd "mt Mod_rc display live"
#电视墙
EXECMD="Tvwall  Decode  Scheme Encode LastChange "
for x in $EXECMD
do
	exec_cmd "mt Mod_tvwall display $x"
done

progress_info "65"

#GB 
exec_cmd "mt Mod_device display SgcLoginCB"
exec_cmd "mt Mod_device display DsmCcbInfo"
exec_cmd "mt Mod_sgs_dsm display sm"
exec_cmd "mt Mod_sgs_ctrl display sip-stack-list"

exec_cmd "mt display IF cb"
exec_cmd "mt display IF all"
exec_cmd "mt display IF memory"
exec_cmd "mt display MSW cb"
exec_cmd "mt display MSW mod"
exec_cmd "mt display MSW event"

exec_cmd "mt mod_mt display tvwallliveinfo"

progress_info "70"

exec_cmd "mt display VOD table"
exec_cmd "mt display VOD all"
exec_cmd "mt display VOD cb"
exec_cmd "mt display VOD backupstatus"
exec_cmd "mt display sr all"
exec_cmd "mt display sr delay"
exec_cmd "mt display sr list"
#sr容错相关信息打印
exec_cmd "mt display sr FT" 

#---------- dccard start -------
if [ "$IS_INTEL_FLAG" -eq 1 ]; then
	for slotId in 1 2                      
	do                                     
		for portId in 0 1 2           
		do                            
			exec_not_show_cmd "drv_vtty $slotId $portId -c cat /var/log/DcCardInfo.log"
			exec_not_show_cmd "drv_vtty $slotId $portId -c rm /var/log/DcCardInfo.log"
		done                       
	done


	if [ ! -f $DCCARD_DIR ]; then                                  
		mkdir -p $DCCARD_DIR                                     
		touch $DCCARD_DIR                  
	fi

	if [ -f /var/log/panic_dc*.log ]; then
		cp -rf  /var/log/panic_dc*.log $DCCARD_DIR
	fi

	mv /var/log/DcCardInfo*.log $DCCARD_DIR
	mv $DCCARD_DIR $INFO_DIR 
fi

#---------- dccard end -------

progress_info "75"


DEVICE_TYPE=$(cat /proc/board_type)
if [ $DEVICE_TYPE = "VMS-B500-A16" ]; then
	###判断是否支持阵列
	MD_MODE=$(mt display ubs runinfo | grep "Dev Mode" | awk '{gsub(/\r/, "");print $4}')
	###判断是否开启了分区模式
	IS_PARTED=$(mt display ubs runinfo | grep "bParted" -m 1 | awk '{gsub(/\r/, "");print $3}')
else
	###判断是否支持阵列
	MD_MODE=$(mt display ubs runinfo | grep "Dev Mode" | awk '{print $4}')
	###判断是否开启了分区模式
	IS_PARTED=$(mt display ubs runinfo | grep "bParted" -m 1 | awk '{print $3}')
fi




if [ $MD_MODE -eq 1 ] || [ $IS_PARTED -eq 1 ]; then
	
	#----------- collect mdcache info --------------
	exec_cmd "cat /sys/mdcache/mgt/info"
	
	exec_cmd "mt display md stat"
	exec_cmd "mt display md all"
	exec_cmd "mt display mdm disk"
	exec_cmd "mt display mdm array"
	
	if [ $IS_INTEL_FLAG -eq 1 ]; then
		MD_NUM=23
	else
		MD_NUM=7;
	fi
	
	i=0
	while true ; do
		if [ $i -gt $MD_NUM ] ; then
			break
		fi
		exec_cmd "mt display md $i"
		i=`expr $i + 1`
	done

	i=0
	while true ; do
		if [ $i -gt $MD_NUM ] ; then
			break
		fi
		exec_cmd "mt display md $i rdev"
		i=`expr $i + 1`
	done

	i=0
	while true ; do
		if [ $i -gt $MD_NUM ] ; then
			break
		fi
		exec_cmd "mt display md $i bmc"
		i=`expr $i + 1`
	done

	i=0
	while true ; do
		if [ $i -gt $MD_NUM ] ; then
			break
		fi
		exec_cmd "mt display md $i bitmap"
		i=`expr $i + 1`
	done

	i=0
	while true ; do
		if [ $i -gt $MD_NUM ] ; then
			break
		fi
		exec_cmd "mt display md $i priv"
		i=`expr $i + 1`
	done
	
	if [ $IS_INTEL_FLAG -eq 1 ]; then
		MDM_NUM=15
	else
		MDM_NUM=7;
	fi
		
	i=0
	while true ; do
		if [ $i -gt $MDM_NUM ] ; then
			break
		fi
		exec_cmd "mt display mdm disk 0 $i"
		i=`expr $i + 1`
	done

	if [ $IS_INTEL_FLAG -eq 1 ]; then
	    i=0
	    while true ; do
		    if [ $i -gt 15 ] ; then
		    	break
		    fi
		    exec_cmd "mt display mdm disk 1 $i"
		    i=`expr $i + 1`
	    done
	    i=0
	    while true ; do
		    if [ $i -gt 15 ] ; then
		    	break
		    fi
		    exec_cmd "mt display mdm disk 2 $i"
		    i=`expr $i + 1`
	    done
	fi
	
	i=0
	while true ; do
		if [ $i -gt $MDM_NUM ] ; then
			break
		fi
		exec_cmd "mt display mdm disk 0 $i super"
		i=`expr $i + 1`
	done
	
	if [ $IS_INTEL_FLAG -eq 1 ]; then
	    i=0
	    while true ; do
		    if [ $i -gt 15 ] ; then
		    	break
		    fi
		    exec_cmd "mt display mdm disk 1 $i super"
		    i=`expr $i + 1`
	    done
	    i=0
	    while true ; do
		    if [ $i -gt 15 ] ; then
		    	break
		    fi
		    exec_cmd "mt display mdm disk 2 $i super"
		    i=`expr $i + 1`
	    done
	fi
	
fi

progress_info "80"

exec_cmd "mt display mc config"
i=0
while true ; do
	if [ $i -gt 7 ] ; then
		break
	fi
	exec_cmd "mt display mc $i"
	i=`expr $i + 1`
done

#pm
exec_cmd "mt display pm disk"
exec_cmd "mt display pm partition"

progress_info "85"

#ubs
EXECMD="runinfo hndl task tsk ic repicinfo dg"
for x in $EXECMD
do
	exec_cmd "mt display ubs $x"
done

exec_cmd "mt display ubs dg 001"
exec_cmd "mt display ubs dg 001 oldest"
exec_cmd "mt display ubs dg 001 chan"
exec_cmd "mt display ubs dg 002 chan"
exec_cmd "mt display ubs dg 003 chan"
exec_cmd "mt display ubs dg 001 disk"
exec_cmd "mt display ubs reccache"
exec_cmd "mt display ubs disk"


if [ $IS_INTEL_FLAG -eq 1 ]; then
    UBS_NUM=15
else
    UBS_NUM=7
fi

i=0
while true ; do
	if [ $i -gt $UBS_NUM ] ; then
		break
	fi
	exec_cmd "mt display ubs disk 0 $i smart"
	i=`expr $i + 1`
done

if [ $IS_INTEL_FLAG -eq 1 ]; then
    i=0
    while true ; do
    	if [ $i -gt 15 ] ; then
    		break
    	fi
    	exec_cmd "mt display ubs disk 1 $i smart"
    	i=`expr $i + 1`
    done

    i=0
    while true ; do
	    if [ $i -gt 15 ] ; then
		    break
	    fi
	    exec_cmd "mt display ubs disk 2 $i smart"
	    i=`expr $i + 1`
    done
fi

#---------- collect hilog info -----------
cat /dev/logmpp 1>> $COMMON_LOG &
tar -czf $COMMON_LOG.tgz $COMMON_LOG
rm $COMMON_LOG
echo "collect systemreport completed."
sleep 1
killall cat 1>/dev/null 2>/dev/null

#----------- tar log-----------------------
echo "exportrunlog..."
DEFAULT_PATH=/var/log/
PGLOG_PATH=/var/log/pg_log
#cp -rf $DEFAULT_PATH/* $INFO_DIR
RUNLOGFILE=var_log.tgz
PGLOGFILE=pg_log.tgz
tar -czvf $INFO_DIR/$RUNLOGFILE $DEFAULT_PATH --exclude=$PGLOG_PATH > /dev/null
tar -czf $INFO_DIR/$PGLOGFILE $PGLOG_PATH > /dev/null
if [[ $GETLOG_PATH == $DEFAULT_PATH* ]]
then
	echo "Match DEFAULT_PATH: $GETLOG_PATH. Not need cp."
else
	echo "No match DEFAULT_PATH: $GETLOG_PATH. Will cp ..."
	cp -rf $GETLOG_PATH*.log $INFO_DIR
	cp -rf $GETLOG_PATH*.debug $INFO_DIR
	cp -rf $GETLOG_PATH*.media $INFO_DIR
	cp -rf $GETLOG_PATH*.cloud $INFO_DIR
	cp -rf $GETLOG_PATH*.tgz $INFO_DIR
	cp -rf $GETLOG_PATH*.gz $INFO_DIR
fi

mv /var/www/nginx/logs/* $INFO_DIR
kill -USR1 `cat /var/www/nginx/conf/nginx.pid`

progress_info "90"

#----------- collect cfg info --------------
cp -rf /var/runtime/cfg $INFO_DIR

#-----------collect reboot time -------------
cp -rf /var/runtime/reboot.log $INFO_DIR

#-----------collect update record -------------
cp -rf /var/runtime/log/update_record.log $INFO_DIR
cp -rf /var/runtime/modify_rootfs.log     $INFO_DIR
cp -rf /var/runtime/update*.log           $INFO_DIR

#-----------collect upgrade log -------------
cp -rf /var/runtime/database/Upgrade.log $INFO_DIR/nddb_Upgrade.log
cp -rf /var/fs_disk/database/Upgrade.log $INFO_DIR/smartdb_Upgrade.log

#-----------collect tvwall live info---------
cp -rf /tmp/tvwall_live_info.txt $INFO_DIR

#-----------collect hostandby info---------
cp -rf /ActiveX/hotstandby/*.log  $INFO_DIR

#-----------collect ezvms log---------
if [ -d "/var/log/VmsClientlog/" ];then
    tar czf  /var/log/VmsClientlog/vmsclient_log.tgz  /var/log/VmsClientlog/* > /dev/null
    mv /var/log/VmsClientlog/vmsclient_log.tgz  $INFO_DIR > /dev/null
fi
#-----------collect dc card log---------
cp -rf /var/logbackup/DcCardInfo*.log   $INFO_DIR

#collect smart info
cp -rf /var/log/smartreport*.tgz $SMARTINFO_DIR > /dev/null 2>&1

progress_info "95"
sleep 5
time1=`date "+%Y%m%d %H-%M-%S"`
echo $time1
tar czf $REPORT_FILE $INFO_DIR > /dev/null
if [ $? -eq 0 ]; then
    echo "CreateReportFileSucess" 
fi

#ktgzfoldersize=`du -k $REPORT_FILE |awk '{print $1}'`
klogbackupfoldersize=`du -k /var/logbackup/0/ |awk '{print $1}'`
#while [ `expr $ktgzfoldersize + $klogbackupfoldersize` -gt 409600 ]
while [ $klogbackupfoldersize -gt $MAXSIZE ]
do
	logfilename=`ls -rt /var/logbackup/0/ | head -n 1`
	if [ ! -z $logfilename ]
	then
		delfilename=/var/logbackup/0/$logfilename
		rm  $delfilename
	fi
	klogbackupfoldersize=`du -k /var/logbackup/0/ |awk '{print $1}'`	
done

delete_pg_log
rm -rf $INFO_DIR
rm -rf /var/log/df_msg.log
rm -rf /var/logbackup/DcCardInfo*.log 
echo "Create**************" 

progress_info "100"

echo "system report collect completely"
