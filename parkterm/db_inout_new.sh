#!/bin/sh

programpath=/
ROOT_PATH=${programpath%%/program*}

DBUSER=postgres
DBNAME=nddb
DBDUMP_METHODB=$ROOT_PATH/program/bin/pg_dump
DBDUMP_RESTORE=$ROOT_PATH/program/bin/pg_restore
DBDUMP_SQL=$ROOT_PATH/program/bin/psql
DB_SQLPATH=$ROOT_PATH/program/sql

DBDUMP_OUTDIR= 
DB_NEWFILE= 

DBDUMP_OUTFILE="db.dump"
DBDUMP_LOGFILE=/var/runtime/imAexport.log

DBLOGNUM=30

if [ -d /var/coredump/database_VMSBK ] || [ -d /var/fs_disk/database_VMSBK ]
then 
    if grep -q "VMS-B500" /proc/board_type; then
        DATABASEDIR=/var/fs_disk/database_VMSBK 
    else
        DATABASEDIR=/var/coredump/database_VMSBK 
    fi
else
    DATABASEDIR=/var/database 
fi 
DB_IMPORTERR_FILEFLAG=$DATABASEDIR/.faildbrestore
PROGRAM_UP_CHKFILE=$DATABASEDIR/.program_updated


###### vmsbk ######
vmsdbexport()
{
    outfile=$1
    # method sql script
    
    if [ -f $DBDUMP_OUTFILE ]; then
        rm -rf $DBDUMP_OUTFILE
    fi

    export PGUSER=postgres
    export PGPASSWORD=123456

    $DBDUMP_METHODB --username=$DBUSER --file=$DBDUMP_OUTFILE --ignore-version -Fc -d $DBNAME 
    unset PGUSER
    unset PGPASSWORD
   
    if [ -f $DBDUMP_OUTFILE ]
    then 
        echo "backup success!" >> $DBDUMP_LOGFILE 
        tar czvf $outfile $DBDUMP_OUTFILE
        echo "tar success!" >> $DBDUMP_LOGFILE 
    else 
        echo "db backup fail!" >> $DBDUMP_LOGFILE 
        exit 1
    fi 
    

    if [ -f $DBDUMP_OUTFILE ]; then
        rm -rf $DBDUMP_OUTFILE
    fi
 
}

###### timingbkdb ######
timingbkdb()
{
    outdir=$1
    filename=$2
    if [ ! -d "$outdir" ]
    then
       mkdir $outdir
    fi
    cd $outdir
    
    echo "=====================================================================" >> $DBDUMP_LOGFILE
    echo "# $(date +%y%m%d%H%M%S) BEGIN : timing backup db:file:$filename" >> $DBDUMP_LOGFILE 
    
    echo "$filename" >> $DBDUMP_LOGFILE 
    vmsdbexport $filename
    
    echo "# $(date +%y%m%d%H%M%S) END   : timing backup db:file:$filename" >> $DBDUMP_LOGFILE 
    echo "=====================================================================" >> $DBDUMP_LOGFILE
    cd -
}


vmsdb_import()
{ 
    filepath=$1

    #get filename
    FileName=`echo ${filepath##*/}`
    
    DumpName=`tar -xvf $FileName`
      
    if [ ! -f "$DumpName" ]; then
        echo "tar -xvf $FileName failed!" >> $DBDUMP_LOGFILE; 
        exit 1;
    fi

    export PGUSER=postgres
    export PGPASSWORD=123456
    export LOCKSQL=/var/runtime/lock.sql

    $DBDUMP_METHODB  --username=$PGUSER --file=$LOCKSQL --ignore-version -Fc -d $DBNAME -t tbl_record_lock -t tbl_lock_block
  
    #drop database
    $DBDUMP_SQL -c "DROP DATABASE \"$DBNAME\"" 2>>$DBDUMP_LOGFILE || { echo "Drop database $DBNAME failed." >> $DBDUMP_LOGFILE; exit 1;}

    touch $DB_IMPORTERR_FILEFLAG

    echo "Drop database OK!" >> $DBDUMP_LOGFILE;

    #creat database
    $DBDUMP_SQL < $DB_SQLPATH/db_create.sql 2>>$DBDUMP_LOGFILE || { echo "Create database failed." >> $DBDUMP_LOGFILE; exit 1;}

    echo "Create database OK!" >> $DBDUMP_LOGFILE;

    #restore
    $DBDUMP_RESTORE  --username=$DBUSER --ignore-version -e -Fc -c --if-exists -d $DBNAME $DumpName 2>>$DBDUMP_LOGFILE || { echo "Restore database failed. [filename $DumpName]" >> $DBDUMP_LOGFILE; exit 1;}

    $DBDUMP_RESTORE  --username=$DBUSER --ignore-version -e -Fc -c --if-exists -d $DBNAME $LOCKSQL

    unset PGUSER
    unset PGPASSWORD

    echo "Restore database OK!" >> $DBDUMP_LOGFILE;

    rm -f $LOCKSQL
    rm -f $DB_IMPORTERR_FILEFLAG

    cd -
}

###### main ######
#dbinout export filepath filename
#dbinout import filepathname

if [ "$1" = "import" ]; then
    echo "import database begin..." >> $DBDUMP_LOGFILE;
    vmsdb_import $2 $3
elif [ "$1" = "export" ]; then
    echo "export database begin..." >> $DBDUMP_LOGFILE;
    DBDUMP_OUTDIR=$2
    DB_NEWFILE=$3.tar.gz
    timingbkdb $DBDUMP_OUTDIR $DB_NEWFILE 
else
  echo "invalid option -- $1"
fi

exit 0
